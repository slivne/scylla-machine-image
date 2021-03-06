#
# Copyright 2020 ScyllaDB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import base64
import json
import shutil
import tempfile
import yaml
import logging
import threading
from textwrap import dedent
from unittest import TestCase
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

sys.path.append('../..')

from lib.log import setup_logging
from aws.scylla_configure import ScyllaAmiConfigurator

LOGGER = logging.getLogger(__name__)


class ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
    """Thread per request HTTP server."""


class BaseHandler(BaseHTTPRequestHandler):
    """HTTP handler that gives metrics from ``core.REGISTRY``."""

    @property
    def response(self):
        return {"/": {"code": 200, "text": ""}}

    def do_GET(self):
        LOGGER.debug("Handling GET: %s", self.path)
        path = self.response.get(self.path)
        response_code = 200  # default
        response_text = ""
        if path:
            response_code = self.response[self.path]["code"]
            response_text = self.response[self.path]["text"]
            LOGGER.info("GET response: %s", response_text)
        self.send_response(code=response_code)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(bytes(response_text, 'UTF-8'))


def http_response_factory(resp_dict):
    class TestHttpResponse(BaseHandler):
        @property
        def response(self):
            return resp_dict
    return TestHttpResponse


class TestHttpServer:
    def __init__(self, port=0, addr="localhost", handler_class=BaseHandler):
        self.port = port
        self.addr = addr
        self.handler_class = handler_class
        self.httpd = None
        self.httpd_thread = None

    def __enter__(self):
        """Starts an HTTP server for prometheus metrics as a daemon thread"""
        self.httpd = ThreadingSimpleServer((self.addr, self.port), self.handler_class)
        self.http_thread = threading.Thread(target=self.httpd.serve_forever)
        self.http_thread.daemon = True
        self.http_thread.start()
        return self.httpd

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestScyllaConfigurator(TestCase):

    def setUp(self):
        LOGGER.info("Setting up test dir")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = Path(self.temp_dir.name)
        setup_logging(log_level=logging.DEBUG, log_dir_path=str(self.temp_dir_path))
        LOGGER.info("Test dir: %s", self.temp_dir_path)
        shutil.copyfile("tests-data/scylla.yaml", str(self.temp_dir_path / "scylla.yaml"))
        self.private_ip = "172.16.16.1"
        self.configurator = ScyllaAmiConfigurator(scylla_yaml_path=str(self.temp_dir_path / "scylla.yaml"))
        self.test_cluster_name = "test-cluster"

    def tearDown(self):
        self.temp_dir.cleanup()

    def default_instance_metadata(self):
        return {"/user-data": {"code": 404, "text": ""},
                "/meta-data/local-ipv4": {"code": 200, "text": self.private_ip}}

    def check_yaml_files_exist(self):
        assert self.configurator.scylla_yaml_example_path.exists(), "scylla.yaml example file not created"
        assert self.configurator.scylla_yaml_path.exists(), "scylla.yaml file not created"

    def run_scylla_configure(self, user_data):
        with TestHttpServer(handler_class=http_response_factory(user_data)) as http_server:
            self.configurator.INSTANCE_METADATA_URL = "http://localhost:%d" % http_server.server_port
            self.configurator.configure_scylla_yaml()

    def test_empty_user_data(self):
        self.run_scylla_configure(user_data=self.default_instance_metadata())
        self.check_yaml_files_exist()
        assert not self.configurator.DISABLE_START_FILE_PATH.exists(), "ami_disabled not created"
        with self.configurator.scylla_yaml_path.open() as scylla_yaml_file:
            LOGGER.info("Testing that defaults are set as expected")
            scyll_yaml = yaml.load(scylla_yaml_file, Loader=yaml.SafeLoader)
            assert scyll_yaml["listen_address"] == self.private_ip
            assert scyll_yaml["broadcast_rpc_address"] == self.private_ip
            assert scyll_yaml["endpoint_snitch"] == "org.apache.cassandra.locator.Ec2Snitch"
            assert scyll_yaml["rpc_address"] == "0.0.0.0"
            assert scyll_yaml["seed_provider"][0]['parameters'][0]['seeds'] == self.private_ip
            assert "scylladb-cluster-" in scyll_yaml["cluster_name"], "Cluster name was not autogenerated"

    def test_user_data_params_are_set(self):
        ip_to_set = "172.16.16.84"
        raw_user_data = json.dumps(
            dict(
                scylla_yaml=dict(
                    cluster_name=self.test_cluster_name,
                    listen_address=ip_to_set,
                    broadcast_rpc_address=ip_to_set,
                    seed_provider=[{
                        "class_name": "org.apache.cassandra.locator.SimpleSeedProvider",
                        "parameters": [{"seeds": ip_to_set}]}],
                )
            )
        )
        self.run_scylla_configure(user_data={"/user-data": {"code": 200, "text": raw_user_data},
                                             "/meta-data/local-ipv4": {"code": 200, "text": self.private_ip}})
        self.check_yaml_files_exist()
        with self.configurator.scylla_yaml_path.open() as scylla_yaml_file:
            scylla_yaml = yaml.load(scylla_yaml_file, Loader=yaml.SafeLoader)
            assert scylla_yaml["cluster_name"] == self.test_cluster_name
            assert scylla_yaml["listen_address"] == ip_to_set
            assert scylla_yaml["broadcast_rpc_address"] == ip_to_set
            assert scylla_yaml["seed_provider"][0]["parameters"][0]["seeds"] == ip_to_set
            # check defaults
            assert scylla_yaml["experimental"] is False
            assert scylla_yaml["auto_bootstrap"] is True

    def test_postconfig_script(self):
        test_file = "scylla_configure_test"
        script = dedent("""
            touch {0.temp_dir_path}/{1}
        """.format(self, test_file))
        raw_user_data = json.dumps(
            dict(
                post_configuration_script=base64.b64encode(bytes(script, "utf-8")).decode("utf-8")
            )
        )
        self.run_scylla_configure(user_data={"/user-data": {"code": 200, "text": raw_user_data},
                                             "/meta-data/local-ipv4": {"code": 200, "text": self.private_ip}})
        self.configurator.run_post_configuration_script()
        assert (self.temp_dir_path / test_file).exists(), "Post configuration script didn't run"

    def test_postconfig_script_with_timeout(self):
        test_file = "scylla_configure_test"
        script_timeout = 5
        script = dedent("""
            sleep {0}
            touch {1.temp_dir_path}/{2}
        """.format(script_timeout, self, test_file))
        raw_user_data = json.dumps(
            dict(
                post_configuration_script=base64.b64encode(bytes(script, "utf-8")).decode("utf-8"),
                post_configuration_script_timeout=script_timeout - 2,
            )
        )

        self.run_scylla_configure(user_data={"/user-data": {"code": 200, "text": raw_user_data},
                                             "/meta-data/local-ipv4": {"code": 200, "text": self.private_ip}})

        with self.assertRaises(expected_exception=SystemExit):
            self.configurator.run_post_configuration_script()
        assert not (self.temp_dir_path / test_file).exists(), "Post configuration script didn't fail with timeout"

    def test_postconfig_script_with_bad_exit_code(self):
        script = dedent("""
            exit 84
        """)
        raw_user_data = json.dumps(
            dict(
                post_configuration_script=base64.b64encode(bytes(script, "utf-8")).decode("utf-8"),
            )
        )
        self.run_scylla_configure(user_data={"/user-data": {"code": 200, "text": raw_user_data},
                                             "/meta-data/local-ipv4": {"code": 200, "text": self.private_ip}})
        with self.assertRaises(expected_exception=SystemExit):
            self.configurator.run_post_configuration_script()

    def test_do_not_start_on_first_boot(self):
        raw_user_data = json.dumps(
            dict(
                start_scylla_on_first_boot=False,
            )
        )
        self.configurator.DISABLE_START_FILE_PATH = self.temp_dir_path / "ami_disabled"
        self.run_scylla_configure(user_data={"/user-data": {"code": 200, "text": raw_user_data},
                                             "/meta-data/local-ipv4": {"code": 200, "text": self.private_ip}})
        self.configurator.start_scylla_on_first_boot()
        assert self.configurator.DISABLE_START_FILE_PATH.exists(), "ami_disabled not created"

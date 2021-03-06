# Copyright (c) 2015-2016 Tigera, Inc. All rights reserved.
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
import json
import logging
import subprocess
import time
from multiprocessing.dummy import Pool as ThreadPool
from pprint import pformat
from unittest import TestCase

import yaml
from deepdiff import DeepDiff

from tests.st.utils.utils import (get_ip, ETCD_SCHEME, ETCD_CA, ETCD_CERT,
                                  ETCD_KEY, debug_failures, ETCD_HOSTNAME_SSL)

HOST_IPV6 = get_ip(v6=True)
HOST_IPV4 = get_ip()

logging.basicConfig(level=logging.DEBUG, format="%(message)s")
logger = logging.getLogger(__name__)

# Disable spammy logging from the sh module
sh_logger = logging.getLogger("sh")
sh_logger.setLevel(level=logging.CRITICAL)

first_log_time = None


class TestBase(TestCase):
    """
    Base class for test-wide methods.
    """

    def setUp(self):
        """
        Clean up before every test.
        """
        self.ip = HOST_IPV4

        # Delete /calico if it exists. This ensures each test has an empty data
        # store at start of day.
        self.curl_etcd("calico", options=["-XDELETE"])

        # Disable Usage Reporting to usage.projectcalico.org
        # We want to avoid polluting analytics data with unit test noise
        self.curl_etcd("calico/v1/config/UsageReportingEnabled",
                       options=["-XPUT -d value=False"])

        # Log a newline to ensure that the first log appears on its own line.
        logger.info("")

    @staticmethod
    def _conn_checker(args):
        source, dest, test_type, result, retries = args
        if test_type == 'icmp':
            if result:
                return source.check_can_ping(dest, retries)
            else:
                return source.check_cant_ping(dest, retries)
        elif test_type == 'tcp':
            if result:
                return source.check_can_tcp(dest, retries)
            else:
                return source.check_cant_tcp(dest, retries)
        elif test_type == 'udp':
            if result:
                return source.check_can_udp(dest, retries)
            else:
                return source.check_cant_udp(dest, retries)
        else:
            logger.error("Unrecognised connectivity check test_type")

    @debug_failures
    def assert_connectivity(self, pass_list, fail_list=None, retries=0,
                            type_list=None):
        """
        Assert partial connectivity graphs between workloads.

        :param pass_list: Every workload in this list should be able to ping
        every other workload in this list.
        :param fail_list: Every workload in pass_list should *not* be able to
        ping each workload in this list. Interconnectivity is not checked
        *within* the fail_list.
        :param retries: The number of retries.
        :param type_list: list of types to test.  If not specified, defaults to
        icmp only.
        """
        if type_list is None:
            type_list = ['icmp', 'tcp', 'udp']
        if fail_list is None:
            fail_list = []

        conn_check_list = []
        for source in pass_list:
            for dest in pass_list:
                if 'icmp' in type_list:
                    conn_check_list.append((source, dest.ip, 'icmp', True, retries))
                if 'tcp' in type_list:
                    conn_check_list.append((source, dest.ip, 'tcp', True, retries))
                if 'udp' in type_list:
                    conn_check_list.append((source, dest.ip, 'udp', True, retries))
            for dest in fail_list:
                if 'icmp' in type_list:
                    conn_check_list.append((source, dest.ip, 'icmp', False, retries))
                if 'tcp' in type_list:
                    conn_check_list.append((source, dest.ip, 'tcp', False, retries))
                if 'udp' in type_list:
                    conn_check_list.append((source, dest.ip, 'udp', False, retries))

        # Empirically, 18 threads works well on my machine!
        check_pool = ThreadPool(18)
        results = check_pool.map(self._conn_checker, conn_check_list)
        check_pool.close()
        check_pool.join()
        # _con_checker should only return None if there is an error in calling it
        assert None not in results, ("_con_checker error - returned None")
        diagstring = ""
        # Check that all tests passed
        if False in results:
            # We've failed, lets put together some diags.
            header = ["source.ip", "dest.ip", "type", "exp_result", "pass/fail"]
            diagstring = "{: >18} {: >18} {: >7} {: >6} {: >6}\r\n".format(*header)
            for i in range(len(conn_check_list)):
                source, dest, test_type, exp_result, retries = conn_check_list[i]
                pass_fail = results[i]
                # Convert pass/fail into an actual result
                if not pass_fail:
                    actual_result = not exp_result
                else:
                    actual_result = exp_result
                diag = [source.ip, dest, test_type, exp_result, actual_result]
                diagline = "{: >18} {: >18} {: >7} {: >6} {: >6}\r\n".format(*diag)
                diagstring += diagline

        assert False not in results, ("Connectivity check error!\r\n"
                                      "Results:\r\n %s\r\n" % diagstring)

    @debug_failures
    def assert_ip_connectivity(self, workload_list, ip_pass_list,
                               ip_fail_list=None, type_list=None):
        """
        Assert partial connectivity graphs between workloads and given ips.

        This function is used for checking connectivity for ips that are
        explicitly assigned to containers when added to calico networking.

        :param workload_list: List of workloads used to check connectivity.
        :param ip_pass_list: Every workload in workload_list should be able to
        ping every ip in this list.
        :param ip_fail_list: Every workload in workload_list should *not* be
        able to ping any ip in this list. Interconnectivity is not checked
        *within* the fail_list.
        :param type_list: list of types to test.  If not specified, defaults to
        icmp only.
        """
        if type_list is None:
            type_list = ['icmp']
        if ip_fail_list is None:
            ip_fail_list = []

        conn_check_list = []
        for workload in workload_list:
            for ip in ip_pass_list:
                if 'icmp' in type_list:
                    conn_check_list.append((workload, ip, 'icmp', True, 0))
                if 'tcp' in type_list:
                    conn_check_list.append((workload, ip, 'tcp', True, 0))
                if 'udp' in type_list:
                    conn_check_list.append((workload, ip, 'udp', True, 0))

            for ip in ip_fail_list:
                if 'icmp' in type_list:
                    conn_check_list.append((workload, ip, 'icmp', False, 0))
                if 'tcp' in type_list:
                    conn_check_list.append((workload, ip, 'tcp', False, 0))
                if 'udp' in type_list:
                    conn_check_list.append((workload, ip, 'udp', False, 0))

        # Empirically, 18 threads works well on my machine!
        check_pool = ThreadPool(18)
        results = check_pool.map(self._conn_checker, conn_check_list)
        check_pool.close()
        check_pool.join()
        # _con_checker should only return None if there is an error in calling it
        assert None not in results, ("_con_checker error - returned None")
        diagstring = ""
        # Check that all tests passed
        if False in results:
            # We've failed, lets put together some diags.
            header = ["source.ip", "dest.ip", "type", "exp_result", "actual_result"]
            diagstring = "{: >18} {: >18} {: >7} {: >6} {: >6}\r\n".format(*header)
            for i in range(len(conn_check_list)):
                source, dest, test_type, exp_result, retries = conn_check_list[i]
                pass_fail = results[i]
                # Convert pass/fail into an actual result
                if not pass_fail:
                    actual_result = not exp_result
                else:
                    actual_result = exp_result
                diag = [source.ip, dest, test_type, exp_result, actual_result]
                diagline = "{: >18} {: >18} {: >7} {: >6} {: >6}\r\n".format(*diag)
                diagstring += diagline

        assert False not in results, ("Connectivity check error!\r\n"
                                      "Results:\r\n %s\r\n" % diagstring)

    def curl_etcd(self, path, options=None, recursive=True):
        """
        Perform a curl to etcd, returning JSON decoded response.
        :param path:  The key path to query
        :param options:  Additional options to include in the curl
        :param recursive:  Whether we want recursive query or not
        :return:  The JSON decoded response.
        """
        if options is None:
            options = []
        if ETCD_SCHEME == "https":
            # Etcd is running with SSL/TLS, require key/certificates
            rc = subprocess.check_output(
                "curl --cacert %s --cert %s --key %s "
                "-sL https://%s:2379/v2/keys/%s?recursive=%s %s"
                % (ETCD_CA, ETCD_CERT, ETCD_KEY, ETCD_HOSTNAME_SSL,
                   path, str(recursive).lower(), " ".join(options)),
                shell=True)
        else:
            rc = subprocess.check_output(
                "curl -sL http://%s:2379/v2/keys/%s?recursive=%s %s"
                % (self.ip, path, str(recursive).lower(), " ".join(options)),
                shell=True)

        return json.loads(rc.strip())

    def check_data_in_datastore(self, host, data, resource, yaml_format=True):
        if yaml_format:
            out = host.calicoctl(
                "get %s --output=yaml" % resource)
            output = yaml.safe_load(out)
        else:
            out = host.calicoctl(
                "get %s --output=json" % resource)
            output = json.loads(out)
        self.assert_same(data, output)

    @staticmethod
    def assert_same(thing1, thing2):
        """
        Compares two things.  Debug logs the differences between them before
        asserting that they are the same.
        """
        assert cmp(thing1, thing2) == 0, \
            "Items are not the same.  Difference is:\n %s" % \
            pformat(DeepDiff(thing1, thing2), indent=2)

    @staticmethod
    def writeyaml(filename, data):
        """
        Converts a python dict to yaml and outputs to a file.
        :param filename: filename to write
        :param data: dictionary to write out as yaml
        """
        with open(filename, 'w') as f:
            text = yaml.dump(data, default_flow_style=False)
            logger.debug("Writing %s: \n%s" % (filename, text))
            f.write(text)

    @staticmethod
    def writejson(filename, data):
        """
        Converts a python dict to json and outputs to a file.
        :param filename: filename to write
        :param data: dictionary to write out as json
        """
        with open(filename, 'w') as f:
            text = json.dumps(data,
                              sort_keys=True,
                              indent=2,
                              separators=(',', ': '))
            logger.debug("Writing %s: \n%s" % (filename, text))
            f.write(text)

    @debug_failures
    def assert_false(self, b):
        """
        Assert false, wrapped to allow debugging of failures.
        """
        assert not b

    @debug_failures
    def assert_true(self, b):
        """
        Assert true, wrapped to allow debugging of failures.
        """
        assert b

    @staticmethod
    def log_banner(msg, *args, **kwargs):
        global first_log_time
        time_now = time.time()
        if first_log_time is None:
            first_log_time = time_now
        time_now -= first_log_time
        elapsed_hms = "%02d:%02d:%02d " % (time_now / 3600,
                                           (time_now % 3600) / 60,
                                           time_now % 60)

        level = kwargs.pop("level", logging.INFO)
        msg = elapsed_hms + str(msg) % args
        banner = "+" + ("-" * (len(msg) + 2)) + "+"
        logger.log(level, "\n" +
                   banner + "\n"
                            "| " + msg + " |\n" +
                   banner)

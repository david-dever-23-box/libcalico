from collections import namedtuple
import json
import socket
import etcd
from netaddr import IPNetwork, IPAddress, AddrFormatError
import os

ETCD_AUTHORITY_DEFAULT = "127.0.0.1:4001"
ETCD_AUTHORITY_ENV = "ETCD_AUTHORITY"

# etcd paths for Calico
CONFIG_PATH = "/calico/config/"
HOSTS_PATH = "/calico/host/"
HOST_PATH = HOSTS_PATH + "%(hostname)s/"
CONTAINER_PATH = HOST_PATH + "workload/docker/%(container_id)s/"
LOCAL_ENDPOINTS_PATH = HOST_PATH + "workload/docker/%(container_id)s/endpoint/"
ALL_ENDPOINTS_PATH = HOSTS_PATH  # Read all hosts
ENDPOINT_PATH = LOCAL_ENDPOINTS_PATH + "%(endpoint_id)s/"
PROFILES_PATH = "/calico/policy/profile/"
PROFILE_PATH = PROFILES_PATH + "%(profile_id)s/"
TAGS_PATH = PROFILE_PATH + "tags"
RULES_PATH = PROFILE_PATH + "rules"
IP_POOL_PATH = "/calico/ipam/%(version)s/pool/"
IP_POOLS_PATH = "/calico/ipam/%(version)s/pool/"

IF_PREFIX = "cali"
"""
prefix that appears in all Calico interface names in the root namespace. e.g.
cali123456789ab.
"""

hostname = socket.gethostname()


class Rule(dict):
    """
    A Calico inbound or outbound traffic rule.
    """

    ALLOWED_KEYS = ["protocol",
                    "src_tag",
                    "src_ports",
                    "src_net",
                    "dst_tag",
                    "dst_ports",
                    "dst_net",
                    "icmp_type",
                    "action"]

    def __init__(self, **kwargs):
        super(Rule, self).__init__()
        for key, value in kwargs.iteritems():
            self[key] = value

    def __setitem__(self, key, value):
        if key not in Rule.ALLOWED_KEYS:
            raise KeyError("Key %s is not allowed on Rule." % key)
        super(Rule, self).__setitem__(key, value)

    def to_json(self):
        return json.dumps(self)


class Rules(namedtuple("Rules", ["id", "inbound_rules", "outbound_rules"])):
    """
    A set of Calico rules describing inbound and outbound network traffic
    policy.
    """

    def to_json(self):
        return json.dumps(self._asdict())


class Endpoint(object):

    def __init__(self, ep_id, state, mac, felix_host):
        self.ep_id = ep_id
        self.state = state
        self.mac = mac

        self.profile_id = None
        self.ipv4_nets = set()
        self.ipv6_nets = set()
        self.ipv4_gateway = None
        self.ipv6_gateway = None

    def to_json(self):
        json_dict = {"state": self.state,
                     "name": IF_PREFIX + self.ep_id[:11],
                     "mac": self.mac,
                     "profile_id": self.profile_id,
                     "ipv4_nets": [str(net) for net in self.ipv4_nets],
                     "ipv6_nets": [str(net) for net in self.ipv6_nets],
                     "ipv4_gateway": str(self.ipv4_gateway) if
                                     self.ipv4_gateway else None,
                     "ipv6_gateway": str(self.ipv6_gateway) if
                                     self.ipv6_gateway else None}
        return json.dumps(json_dict)

    @classmethod
    def from_json(cls, ep_id, json_str):
        json_dict = json.loads(json_str)
        ep = cls(ep_id=ep_id,
                 state=json_dict["state"],
                 mac=json_dict["mac"],
                 felix_host=["hostname"])
        for net in json_dict["ipv4_nets"]:
            ep.ipv4_nets.add(IPNetwork(net))
        for net in json_dict["ipv6_nets"]:
            ep.ipv6_nets.add(IPNetwork(net))
        ipv4_gw = json_dict["ipv4_gateway"]
        if ipv4_gw:
            ep.ipv4_gateway = IPAddress(ipv4_gw)
        ipv6_gw = json_dict["ipv6_gateway"]
        if ipv6_gw:
            ep.ipv6_gateway = IPAddress(ipv6_gw)
        ep.profile_id = json_dict["profile_id"]
        return ep


class Vividict(dict):
    # From http://stackoverflow.com/a/19829714
    def __missing__(self, key):
        value = self[key] = type(self)()
        return value


class DatastoreClient(object):
    """
    An datastore client that exposes high level Calico operations needed by the
    calico CLI.
    """

    def __init__(self):
        etcd_authority = os.getenv(ETCD_AUTHORITY_ENV, ETCD_AUTHORITY_DEFAULT)
        (host, port) = etcd_authority.split(":", 1)
        self.etcd_client = etcd.Client(host=host, port=int(port))

    def create_global_config(self):
        config_dir = CONFIG_PATH
        try:
            self.etcd_client.read(config_dir)
        except KeyError:
            # Didn't exist, create it now.
            self.etcd_client.set(config_dir + "InterfacePrefix", IF_PREFIX)
            self.etcd_client.set(config_dir + "LogSeverityFile", "DEBUG")

    def create_host(self, bird_ip, bird6_ip):
        """
        Create a new Calico host.

        :param bird_ip: The IP address BIRD should listen on.
        :return: nothing.
        """
        host_path = HOST_PATH % {"hostname": hostname}
        # Set up the host
        self.etcd_client.write(host_path + "bird_ip", bird_ip)
        self.etcd_client.write(host_path + "bird6_ip", bird6_ip)
        self.etcd_client.set(host_path + "config/marker", "created")
        workload_dir = host_path + "workload"
        try:
            self.etcd_client.read(workload_dir)
        except KeyError:
            # Didn't exist, create it now.
            self.etcd_client.write(workload_dir, None, dir=True)
        return

    def remove_host(self):
        """
        Remove a Calico host.
        :return: nothing.
        """
        host_path = HOST_PATH % {"hostname": hostname}
        try:
            self.etcd_client.delete(host_path, dir=True, recursive=True)
        except KeyError:
            pass

    def get_groups_by_endpoint(self, endpoint_id):
        return []   # TODO

    def get_ip_pools(self, version):
        """
        Get the configured IP pools.

        :param version: "v4" for IPv4, "v6" for IPv6
        :return: List of netaddr.IPNetwork IP pools.
        """
        assert version in ("v4", "v6")
        pools = []
        try:
            pools = self._get_ip_pools_with_keys(version).keys()
        except KeyError:
            # No pools defined yet, return empty list.
            pass

        return pools

    def _get_ip_pools_with_keys(self, version):
        """
        Get configured IP pools with their etcd keys.

        :param version: "v4" for IPv4, "v6" for IPv6
        :return: dict of {<IPNetwork>: <etcd key>} for the pools.
        """
        pool_path = IP_POOLS_PATH % {"version": version}
        try:
            nodes = self.etcd_client.read(pool_path).children
        except KeyError:
            # Path doesn't exist.  Interpret as no configured pools.
            return {}
        else:
            pools = {}
            for child in nodes:
                cidr = child.value
                if cidr:
                    pool = IPNetwork(cidr)
                    pools[pool] = child.key
            return pools

    def add_ip_pool(self, version, pool):
        """
        Add the given pool to the list of IP allocation pools.  If the pool already exists, this
        method completes silently without modifying the list of pools.

        :param version: "v4" for IPv4, "v6" for IPv6
        :param pool: IPNetwork object representing the pool
        :return: None
        """
        assert version in ("v4", "v6")
        assert isinstance(pool, IPNetwork)

        # Normalize to CIDR format (i.e. 10.1.1.1/8 goes to 10.0.0.0/8)
        pool = pool.cidr

        # Check if the pool exists.
        if pool in self.get_ip_pools(version):
            return

        pool_path = IP_POOL_PATH % {"version": version}
        self.etcd_client.write(pool_path, str(pool), append=True)

    def del_ip_pool(self, version, pool):
        """
        Delete the given CIDR range from the list of pools.  If the pool does not exist, raise a
        etcd.EtcdKeyNotFound:.

        :param version: "v4" for IPv4, "v6" for IPv6
        :param pool: IPNetwork object representing the pool
        :return: None
        """
        assert version in ("v4", "v6")
        assert isinstance(pool, IPNetwork)

        pools = self._get_ip_pools_with_keys(version)
        try:
            key = pools[pool.cidr]
            self.etcd_client.delete(key)
        except KeyError:
            # Re-raise with a better error message.
            raise KeyError("%s is not a configured IP pool." % pool)

    def group_exists(self, name):
        """
        Check if a group exists.

        :param name: The name of the group.
        :return: True if the group exists, false otherwise.
        """
        profile_path = PROFILE_PATH % {"profile_id": name}
        try:
            _ = self.etcd_client.read(profile_path)
        except KeyError:
            return False
        else:
            return True

    def create_group(self, name):
        """
        Create a security group.  In this implementation, security groups
        accept traffic only from themselves, but can send traffic anywhere.

        Note this will clobber any existing group with this name.

        :param name: Unique string name for the group.
        :return: nothing.
        """
        # A group is a implemented as a policy profile with a self-referencing
        # tag.
        profile_path = PROFILE_PATH % {"profile_id": name}
        self.etcd_client.write(profile_path + "tags", '["%s"]' % name)

        # Accept inbound traffic from self, allow outbound traffic to anywhere.
        default_deny = Rule(action="deny")
        accept_self = Rule(src_tag=name)
        default_allow = Rule(action="allow")
        rules = Rules(id=name,
                      inbound_rules=[accept_self, default_deny],
                      outbound_rules=[default_allow])
        self.etcd_client.write(profile_path + "rules", rules.to_json())

    def delete_group(self, name):
        """
        Delete a security group with a given name.

        :param name: Unique string name for the group.
        :return: the ID of the group that was deleted, or None if the group
        couldn't be found.
        """

        profile_path = PROFILE_PATH % {"profile_id": name}
        self.etcd_client.delete(profile_path, recursive=True, dir=True)
        return

    def get_groups(self):
        """
        Get the all configured groups.
        :return: a set of group names
        """
        groups = set()
        try:
            etcd_groups = self.etcd_client.read(PROFILES_PATH,
                                                recursive=True,).children
            for child in etcd_groups:
                packed = child.key.split("/")
                if len(packed) > 4:
                    groups.add(packed[4])
        except KeyError:
            # Means the GROUPS_PATH was not set up.  So, group does not exist.
            pass
        return groups

    def get_group_members(self, name):
        """
        Get the all configured groups.

        :param name: Unique string name of the group.
        :return: a list of members
        """
        members = []
        try:
            endpoints = self.etcd_client.read(ALL_ENDPOINTS_PATH,
                                              recursive=True)
        except KeyError:
            # Means the ALL_ENDPOINTS_PATH was not set up.  So, group has no
            # members because there are no endpoints.
            return members

        for child in endpoints.leaves:
            packed = child.key.split("/")
            if len(packed) == 9:
                ep_id = packed[-1]
                ep = Endpoint.from_json(ep_id, child.value)
                if ep.profile_id == name:
                    members.append(ep.ep_id)
        return members

    def add_workload_to_group(self, group_name, container_id):
        endpoint_id = self.get_ep_id_from_cont(container_id)

        # Change the profile on the endpoint.
        ep = self.get_endpoint(hostname, container_id, endpoint_id)
        ep.profile_id = group_name
        self.set_endpoint(hostname, container_id, ep)

    def remove_workload_from_group(self, container_id):
        endpoint_id = self.get_ep_id_from_cont(container_id)

        # Change the profile on the endpoint.
        ep = self.get_endpoint(hostname, container_id, endpoint_id)
        ep.profile_id = None
        self.set_endpoint(hostname, container_id, ep)

    def get_ep_id_from_cont(self, container_id):
        """
        Get a single endpoint ID from a container ID.

        :param container_id: The Docker container ID.
        :return: Endpoint ID as a string.
        """
        ep_path = LOCAL_ENDPOINTS_PATH % {"hostname": hostname,
                                          "container_id": container_id}
        try:
            endpoints = self.etcd_client.read(ep_path).leaves
        except KeyError:
            # Re-raise with better message
            raise KeyError("Container with ID %s was not found." % container_id)

        # Get the first endpoint & ID
        endpoint = endpoints.next()
        (_, _, _, _, _, _, _, _, endpoint_id) = endpoint.key.split("/", 8)
        return endpoint_id

    def get_endpoint(self, hostname, container_id, endpoint_id):
        """
        Get all of the details for a single endpoint.

        :param endpoint_id: The ID of the endpoint
        :return:  an Endpoint Object
        """
        ep_path = ENDPOINT_PATH % {"hostname": hostname,
                                   "container_id": container_id,
                                   "endpoint_id": endpoint_id}
        ep_json = self.etcd_client.read(ep_path).value
        ep = Endpoint.from_json(endpoint_id, ep_json)
        return ep

    def set_endpoint(self, hostname, container_id, endpoint):
        """
        Write a single endpoint object to the datastore.

        :param hostname: The hostname for the Docker hosting this container.
        :param container_id: The Docker container ID.
        :param endpoint: The Endpoint to add to the container.
        """
        ep_path = ENDPOINT_PATH % {"hostname": hostname,
                                   "container_id": container_id,
                                   "endpoint_id": endpoint.ep_id}
        self.etcd_client.write(ep_path, endpoint.to_json())


    def get_hosts(self):
        """
        Get the all configured hosts
        :return: a dict of hostname => {
                               type => {
                                   container_id => {
                                       endpoint_id => {
                                           "addrs" => addr,
                                           "mac" => mac,
                                           "state" => state
                                       }
                                   }
                               }
                           }
        """
        hosts = Vividict()
        try:
            etcd_hosts = self.etcd_client.read('/calico/host',
                                               recursive=True).leaves
            for child in etcd_hosts:
                packed = child.key.split("/")
                if len(packed) > 4 and len(packed) < 9:
                    (_, _, _, host, _) = packed[0:5]
                    if not hosts[host]:
                        hosts[host] = Vividict()
                elif len(packed) == 9:
                    (_, _, _, host, _, container_type, container_id, _,
                     endpoint_id) = packed
                    ep = Endpoint.from_json(endpoint_id, child.value)
                    ep_dict = hosts[host][container_type][container_id]\
                        [endpoint_id]
                    ep_dict["addrs"] = [str(net) for net in
                                        ep.ipv4_nets | ep.ipv6_nets]
                    ep_dict["mac"] = str(ep.mac)
                    ep_dict["state"] = ep.state

        except KeyError:
            pass

        return hosts

    def get_default_next_hops(self, hostname):
        """
        Get the next hop IP addresses for default routes on the given host.

        :param hostname: The hostname for which to get default route next hops.
        :return: Dict of {ip_version: IPAddress}
        """

        host_path = HOST_PATH % {"hostname": hostname}
        ipv4 = self.etcd_client.read(host_path + "bird_ip").value
        ipv6 = self.etcd_client.read(host_path + "bird6_ip").value

        next_hops = {}

        # The IP addresses read from etcd could be blank. Only store them if
        # they can be parsed by IPAddress
        try:
            next_hops[4] = IPAddress(ipv4)
        except AddrFormatError:
            pass

        try:
            next_hops[6] = IPAddress(ipv6)
        except AddrFormatError:
            pass

        return next_hops


    def remove_all_data(self):
        """
        Remove all data from the datastore.

        We don't care if Calico data can't be found.

        """
        try:
            self.etcd_client.delete("/calico", recursive=True, dir=True)
        except KeyError:
            pass

    def remove_container(self, container_id):
        container_path = CONTAINER_PATH % {"hostname": hostname,
                                           "container_id": container_id}
        self.etcd_client.delete(container_path, recursive=True, dir=True)




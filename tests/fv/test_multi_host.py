from time import sleep
import os
import sh
from sh import docker

pwd = sh.pwd().stdout.rstrip()

docker_exec = docker.bake("exec")
host1_exec = docker_exec.bake("-t", "host1", "bash", "-x", "-c")
host2_exec = docker_exec.bake("-t", "host2", "bash", "-c")

# We *must* remove all inner containers and images before removing the outer
# container. Otherwise the inner images will stick around and fill disk.
# https://github.com/jpetazzo/dind#important-warning-about-disk-usage
try:
    host1_exec('docker rm -f $(docker ps -qa) ; docker rmi $(docker images -qa)')
except sh.ErrorReturnCode_1:
    pass
try:
    host2_exec('docker rm -f $(docker ps -qa) ; docker rmi $(docker images -qa)')
except sh.ErrorReturnCode_1:
    pass
print "Containers and images within host containers removed."

try:
    sh.docker.rm("-f", "host1", "host2")
except sh.ErrorReturnCode:
    pass
print "Host containers removed."

# # Save and load each image, so we can use them in the inner host containers.
print sh.bash("./build_node.sh").stdout
docker.save("--output", "calico-node.tar", "calico/node")
if not os.path.isfile("busybox.tar"):
    docker.pull("busybox:latest")
    docker.save("--output", "busybox.tar", "busybox:latest")
if not os.path.isfile("nsenter.tar"):
    docker.pull("jpetazzo/nsenter:latest")
    docker.save("--output", "nsenter.tar", "jpetazzo/nsenter:latest")
if not os.path.isfile("etcd.tar"):
    docker.pull("quay.io/coreos/etcd:v2.0.10")
    docker.save("--output", "etcd.tar", "quay.io/coreos/etcd:v2.0.10")

print sh.bash("./create_binary.sh")

print docker.run("--privileged", "-v", pwd+":/code", "--name", "host1", "-tid", "jpetazzo/dind")
docker.run("--privileged", "-v", pwd+":/code", "--name", "host2", "-tid", "jpetazzo/dind")

host1_ip = docker.inspect("--format", "'{{ .NetworkSettings.IPAddress }}'", "host1").stdout.rstrip()
host2_ip = docker.inspect("--format", "'{{ .NetworkSettings.IPAddress }}'", "host2").stdout.rstrip()

host1_exec("while ! docker ps; do sleep 1; done && "
           "docker load --input /code/calico-node.tar && "
           "docker load --input /code/busybox.tar && "
           "docker load --input /code/nsenter.tar && "
           "docker load --input /code/etcd.tar")

host2_exec("while ! docker ps; do sleep 1; done && "
           "docker load --input /code/calico-node.tar && "
           "docker load --input /code/busybox.tar && "
           "docker load --input /code/nsenter.tar")

cmd = ("--name calico "
      "--advertise-client-urls http://%s:2379 "
      "--listen-client-urls http://0.0.0.0:2379 "
      "--initial-advertise-peer-urls http://%s:2380 "
      "--listen-peer-urls http://0.0.0.0:2380 "
      "--initial-cluster-token etcd-cluster-2 "
      "--initial-cluster calico=http://%s:2380 "
      "--initial-cluster-state new" % (host1_ip, host1_ip, host1_ip))
host1_exec('docker run -d -p 2379:2379 quay.io/coreos/etcd:v2.0.10 %s' % cmd)


calicoctl = "/code/dist/calicoctl"
etcd_port = "ETCD_AUTHORITY=%s:2379" % host1_ip
host1_listen = docker_exec.bake("-i", "host1", "bash", "-s")
host2_listen = docker_exec.bake("-i", "host2", "bash", "-s")

host1_listen(_in="%s %s reset || true" % (etcd_port, calicoctl))

host1_listen(_in="%s %s node --ip=%s" % (etcd_port, calicoctl, host1_ip))
host2_listen(_in="%s %s node --ip=%s" % (etcd_port, calicoctl, host2_ip))


calico_port = "DOCKER_HOST=localhost:2377"

sleep(1)

host1_listen(_in="%s docker run -e CALICO_IP=192.168.1.1 --name workload-A -tid busybox" % (calico_port))
host1_listen(_in="%s docker run -e CALICO_IP=192.168.1.2 --name workload-B -tid busybox" % (calico_port))
host1_listen(_in="%s docker run -e CALICO_IP=192.168.1.3 --name workload-C -tid busybox" % (calico_port))

host2_listen(_in="%s docker run -e CALICO_IP=192.168.1.4 --name workload-D -tid busybox" % (calico_port))
host2_listen(_in="%s docker run -e CALICO_IP=192.168.1.5 --name workload-E -tid busybox" % (calico_port))

host1_listen(_in="%s %s profile add PROF_A_C_E" % (etcd_port, calicoctl))
host1_listen(_in="%s %s profile add PROF_B" % (etcd_port, calicoctl))
host1_listen(_in="%s %s profile add PROF_D" % (etcd_port, calicoctl))

host1_listen(_in="%s %s profile PROF_A_C_E member add workload-A" % (etcd_port, calicoctl))
host1_listen(_in="%s %s profile PROF_B member add workload-B" % (etcd_port, calicoctl))
host1_listen(_in="%s %s profile PROF_A_C_E member add workload-C" % (etcd_port, calicoctl))

host2_listen(_in="%s %s profile PROF_D member add workload-D" % (etcd_port, calicoctl))
host2_listen(_in="%s %s profile PROF_A_C_E member add workload-E" % (etcd_port, calicoctl))

sleep(1)

host1_exec("docker exec workload-A ping -c 4 192.168.1.3")

try:
    host1_exec("docker exec workload-A ping -c 4 192.168.1.2")
    raise
except sh.ErrorReturnCode_1:
    pass

try:
    host1_exec("docker exec workload-A ping -c 4 192.168.1.4")
    raise
except sh.ErrorReturnCode_1:
    pass

host1_exec("docker exec workload-A ping -c 4 192.168.1.5")


try:
    host1_exec('docker rm -f $(docker ps -qa) ; docker rmi $(docker images -qa)')
except sh.ErrorReturnCode_1:
    pass
try:
    host2_exec('docker rm -f $(docker ps -qa) ; docker rmi $(docker images -qa)')
except sh.ErrorReturnCode_1:
    pass
print "Containers and images within host containers removed."
try:
    sh.docker.rm("-f", "host1", "host2")
except sh.ErrorReturnCode:
    pass
print "Host containers removed."

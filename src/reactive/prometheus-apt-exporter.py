#!/usr/bin/python3
"""Installs and configures prometheus-apt-exporter."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.core import hookenv, host

from charms.layer import snap
from charms.reactive import (
    endpoint_from_flag,
    hook,
    remove_state,
    set_state,
    when,
    when_all,
    when_any,
    when_not,
    when_not_all,
)


DASHBOARD_PATH = os.getcwd() + "/files/grafana-dashboards"
SNAP_NAME = "prometheus-apt-exporter"
#SVC_NAME = "snap.prometheus-apt-exporter.daemon"
SVC_NAME = "snap.prometheus-apt-exporter.apt-exporter.service"
PORT_NUMBER = "8089"
LIBVIRTD_APPARMOR_LOCAL_PROFILE = "/etc/apparmor.d/local/usr.sbin.libvirtd"


@when("juju-info.connected")
@when_not_all("apt-exporter.installed", "apt-exporter.started")
def install_packages():
    """Installs the prometheus-apt-exporter snap."""
    hookenv.status_set("maintenance", "Installing software")
    config = hookenv.config()
    channel = config.get("snap_channel")
    #snap.install(SNAP_NAME, channel=channel, force_dangerous=False)
    subprocess.check_call(["wget", "http://10.5.2.203:8000/prometheus-apt-exporter_0.2.5_amd64.snap"])
    subprocess.check_call(["snap", "install", "prometheus-apt-exporter_0.2.5_amd64.snap", "--dangerous"])
    subprocess.check_call(["snap", "connect", "prometheus-apt-exporter:apt-exporter-files"])

    # LP#1954934: silence libvirtd ptrace apparmor denials
    #configure_libvirtd_apparmor_local_profile(LIBVIRTD_APPARMOR_LOCAL_PROFILE)

    hookenv.status_set("active", "Exporter installed and connected")
    #TODO
    hookenv.open_port(PORT_NUMBER)
    set_state("apt-exporter.installed")


@hook("upgrade-charm")
def upgrade():
    """Reset the install state on upgrade, to ensure resource extraction."""
    hookenv.status_set("maintenance", "Charm upgrade in progress")
    remove_state("apt-exporter.installed")
    remove_state("apt-exporter.started")
    remove_state("apt-exporter.dashboard-registered")
    update_dashboards_from_resource()
    register_grafana_dashboards()


@when_not("apt-exporter.started")
@when_any("apt-exporter.installed", "config.changed")
def start_snap():
    """Configure snap.prometheus-apt-exporter.apt-exporter service."""
    if not host.service_running(SVC_NAME):
        hookenv.status_set("maintenance", "Service is down, starting")
        hookenv.log("Service {} is down, starting...".format(SVC_NAME))
        host.service_start(SVC_NAME)
        hookenv.status_set("active", "Service started")
        hookenv.log("start_snap() Service started")
    else:
        hookenv.status_set("active", "Ready")
        set_state("apt-exporter.started")
        hookenv.log("start_snap() apt-exporter.started")

    update_dashboards_from_resource()

    hookenv.log("Installed and set flag apt-exporter.started")


@when("config.changed.snap_channel")
def snap_channel_changed():
    """Remove the state apt.exporter.installed if the snap channel changes."""
    remove_state("apt-exporter.installed")
    remove_state("apt-exporter.started")


@when_all("apt-exporter.started", "scrape.available")
def configure_scrape_relation(scrape_service):
    """Connect prometheus to the the exporter for consumption."""
    scrape_service.configure(PORT_NUMBER)
    remove_state("apt-exporter.configured")


@when("nrpe-external-master.changed")
def nrpe_changed():
    """Trigger nrpe update."""
    remove_state("apt-exporter.configured")


@when("apt-exporter.changed")
def prometheus_changed():
    """Trigger prometheus update."""
    remove_state("apt-exporter.prometheus_relation_configured")
    remove_state("apt-exporter.configured")


@when("nrpe-external-master.available")
@when_not("apt-exporter.configured")
def update_nrpe_config(svc):
    """Configure the nrpe check for the service."""
    if not os.path.exists("/var/lib/nagios"):
        hookenv.status_set("blocked", "Waiting for nrpe package installation")
        return

    hookenv.status_set("maintenance", "Configuring nrpe checks")

    hostname = nrpe.get_nagios_hostname()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    # check / instead of /metrics to avoid lp:1883549, expect status code 200
    nrpe_setup.add_check(
        shortname="prometheus_apt_exporter_http",
        check_cmd="check_http -I 127.0.0.1 -p {} -u / -e 200".format(PORT_NUMBER),
        description="Prometheus Libvirt Exporter HTTP check",
    )
    nrpe_setup.write()
    hookenv.status_set("active", "ready")
    set_state("apt-exporter.configured")


@when("apt-exporter.installed")
@when_not("juju-info.available")
def remove_apt_exporter():
    """Uninstall the snap."""
    remove_state("apt-exporter.installed")
    remove_state("apt-exporter.started")
    snap.remove(SNAP_NAME)


@when("apt-exporter.configured")
@when_not("nrpe-external-master.available")
def remove_nrpe_check():
    """Remove the nrpe check."""
    hostname = nrpe.get_nagios_hostname()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe_setup.remove_check(shortname="prometheus_apt_exporter_http")
    remove_state("apt-exporter.configured")


@when_all("leadership.is_leader", "endpoint.dashboards.joined")
@when_not("apt-exporter.dashboard-registered")
def register_grafana_dashboards():
    """After joining to grafana, push the dashboard.

    Along with the dashboard, the current juju model name is transmitted
    as well. This enables grafana to detect CMR deployments (and possibly
    specific handling for dashboards coming from foreign models).
    """
    grafana_endpoint = endpoint_from_flag("endpoint.dashboards.joined")

    if grafana_endpoint is None:
        hookenv.log("register_grafana_dashboard: no grafana endpoint available")
        return

    hookenv.log("register_grafana_dashboard: grafana relation joined, push dashboard")

    # load pre-distributed dashboards, that may have been overwritten by resource
    dash_dir = Path(DASHBOARD_PATH)
    for dash_file in dash_dir.glob("*.json"):
        dashboard = dash_file.read_text()
        digest = hashlib.md5(dashboard.encode("utf8")).hexdigest()
        dash_dict = json.loads(dashboard)
        dash_dict["digest"] = digest
        dash_dict["source_model"] = hookenv.model_name()
        grafana_endpoint.register_dashboard(dash_file.stem, dash_dict)
        hookenv.log(
            "register_grafana_dashboard: pushed {}, digest {}".format(dash_file, digest)
        )
        set_state("apt-exporter.dashboard-registered")


def update_dashboards_from_resource():
    """Extract resource zip file into templates directory."""
    dashboards_zip_resource = hookenv.resource_get("dashboards")
    if not dashboards_zip_resource:
        hookenv.log("No dashboards resource found", hookenv.DEBUG)
        # no dashboards zip found, go with the default distributed dashboard
        return

    hookenv.log("Installing dashboards from resource", hookenv.DEBUG)
    try:
        shutil.copy(dashboards_zip_resource, DASHBOARD_PATH)
    except IOError as error:
        hookenv.log("Problem copying resource: {}".format(error), hookenv.ERROR)
        return

    try:
        with ZipFile(dashboards_zip_resource, "r") as zipfile:
            zipfile.extractall(path=DASHBOARD_PATH)
            hookenv.log("Extracted dashboards from resource", hookenv.DEBUG)
    except BadZipFile as error:
        hookenv.log("BadZipFile: {}".format(error), hookenv.ERROR)
        return
    except PermissionError as error:
        hookenv.log(
            "Unable to unzip the provided resource: {}".format(error), hookenv.ERROR
        )
        return

    register_grafana_dashboards()


#def configure_libvirtd_apparmor_local_profile(libvirtd_apparmor_local_profile):
#    """Silence libvirtd ptrace apparmor denials from kern.log."""
#    deny_ptrace_rule = (
#        "deny ptrace (read) peer=snap.prometheus-libvirt-exporter.daemon,"
#    )
#
#    # if there is no libvirtd installed this is a noop
#    if not os.path.exists(libvirtd_apparmor_local_profile):
#        return
#
#    # Read current local profile and strip new lines.
#    current_profile_lines = open(libvirtd_apparmor_local_profile, "r").readlines()
#    current_profile_lines = list(map(str.strip, current_profile_lines))
#
#    # If deny ptrace rule is already there do nothing.
#    if deny_ptrace_rule in current_profile_lines:
#        return
#
#    # Add ptrace deny rule
#    open(libvirtd_apparmor_local_profile, "a").write("\n" + deny_ptrace_rule + "\n")
#
#    # Reload libvirtd apparmor profile
#    libvirtd_apparmor_profile = "/etc/apparmor.d/usr.sbin.libvirtd"
#    subprocess.check_call(["apparmor_parser", "-r", libvirtd_apparmor_profile])

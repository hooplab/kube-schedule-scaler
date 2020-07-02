#!/usr/bin/env python3
""" Main module of kube-schedule-scaler """
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from time import sleep
from typing import Optional, NewType, NamedTuple, List, Dict

import dateutil.tz
from kubernetes import client, config, watch
from kubernetes.config.config_exception import ConfigException
from croniter import croniter


class RawScalingSchedule(NamedTuple):
    schedule: str
    replicas: Optional[str]


class ScalingSchedule(NamedTuple):
    schedule: str
    replicas: int


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
SCHEDULED_SCALING_LOG_LEVEL = os.environ.get("SCHEDULED_SCALING_LOG_LEVEL", LOG_LEVEL)
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
DRY_RUN = True if os.environ.get("DRY_RUN", "").lower() == "true" else False
PREDEFINED_SCHEDULES_JSON = os.environ.get("PREDEFINED_SCHEDULES", "{}")

tz = dateutil.tz.gettz(TIMEZONE)

# global log config
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
    datefmt='%d-%m-%Y %H:%M:%S'
)

# configure module logger
logger = logging.getLogger("schedule_scaling")
logger.setLevel(SCHEDULED_SCALING_LOG_LEVEL)

# load kubernetes config
try:
    config.load_kube_config()
except ConfigException as e:
    logger.debug("could not load kubernetes config from file system. loading in-cluster config.")
    config.load_incluster_config()

# set api client
api = client.AppsV1Api()


def int_or_fail(value: str) -> int:
    if value.isdigit():
        return int(value)
    raise ValueError("expected value '{}' to be only digits".format(value))


def resolve_value(value_or_pointer: Optional[str], annotations: dict) -> Optional[int]:
    if value_or_pointer is not None:
        if value_or_pointer.isdigit():
            # it's an actual value
            return value_or_pointer
        # resolve the pointer value
        return annotations.get(value_or_pointer, None)
    return None


def resolve_scaling_values(action: RawScalingSchedule, annotations: dict, deployment: str) -> ScalingSchedule:
    replicas_resolved = resolve_value(action.replicas, annotations)
    if replicas_resolved is None:
        raise ValueError("{} - value of replicas or value of annotation replicas points at is None: {}".format(deployment, action.replicas))
    return ScalingSchedule(
        schedule=action.schedule,
        replicas=int_or_fail(replicas_resolved)
    )


def parse_schedules(schedules_json: str, deployment: str) -> List[RawScalingSchedule]:
    """ Parse the JSON schedule """
    try:
        parsed = json.loads(schedules_json)
        if not isinstance(parsed, list):
            raise TypeError("annotation hoopla/scaling.schedule is not a json list")
        return [RawScalingSchedule(**v) for v in parsed]
    except (json.decoder.JSONDecodeError) as err:
        logger.error("{} - Error in parsing JSON {}".format(deployment, schedules_json))
        logger.exception(err)
        return []


def get_predefined_schedule(name: str, predefined_schedules: Dict[str, List[RawScalingSchedule]]) -> List[RawScalingSchedule]:
    schedule = predefined_schedules.get(name, None)
    if schedule is None:
        raise KeyError("could not find predefined schedule named '{}'".format(name))
    return schedule


def deployments_to_scale(predefined_schedules: Dict[str, List[RawScalingSchedule]]) -> List[ScalingSchedule]:
    """ Getting the deployments configured for schedule scaling """
    scaling_dict = {}

    deployments = api.list_deployment_for_all_namespaces(pretty=True).items

    for deployment in deployments:
        namespace = deployment.metadata.namespace
        deployment_name = deployment.metadata.name
        f_deployment = "{}/{}".format(namespace, deployment_name)

        annotations = deployment.metadata.annotations
        schedule_name = annotations.get("hoopla/scaling.schedule.predefined", None)

        try:
            if schedule_name is not None:
                scaling_schedule = get_predefined_schedule(schedule_name, predefined_schedules)
                logger.debug("{} - found predefined schedule '{}'".format(f_deployment, schedule_name))
            else:
                scaling_schedule = parse_schedules(annotations.get("hoopla/scaling.schedule", "[]"), f_deployment)
                logger.debug("{} - found schedule".format(f_deployment))
        except Exception as e:
            logger.error("{} - could not load schedule".format(f_deployment))
            logger.exception(e)
            scaling_schedule = []

        if scaling_schedule is None or len(scaling_schedule) == 0:
            continue

        disabled_str = annotations.get("hoopla/scaling.disabled", "false")
        if disabled_str.lower() == "true":
            logger.info("scheduled scaling for '{}' is disabled because value of annotation 'hoopla/scaling.disabled' is 'true'", f_deployment)
            continue

        # replace annotation pointers with actual values
        try:
            scaling_dict[f_deployment] = [resolve_scaling_values(action, annotations, f_deployment) for action in scaling_schedule]
        except ValueError as e:
            logger.error("{} - could not resolve values in one or more of the schedules".format(f_deployment))
            logger.exception(e)

    if len(scaling_dict.items()) == 0:
        logger.info("No deployment is configured for schedule scaling")

    return scaling_dict


def get_delta_sec(schedule_expr: str) -> int:
    """ Returns the number of seconds passed since last occurence of the given cron expression """
    # get current time
    now = datetime.now(tz=tz)
    # get the last previous occurrence of the cron expression
    time = croniter(schedule_expr, now).get_prev()
    # convert now to unix timestamp
    timestamp = now.timestamp()
    # return the delta
    return timestamp - time


def get_wait_sec() -> int:
    """ Return the number of seconds to wait before the next minute """
    now = datetime.now()
    future = datetime(now.year, now.month, now.day, now.hour, now.minute) + timedelta(minutes=1)
    return (future - now).total_seconds()


def dry_run_arg(dry_run: bool):
    return dict(dry_run="All") if dry_run else dict()


def dry_run_prefix(dry_run: bool) -> str:
    return "[DRY RUN] " if dry_run else ""


def process_deployment(deployment: str, schedules: List[ScalingSchedule], dry_run: bool):
    """ Determine actions to run for the given deployment and list of schedules """
    namespace, name = deployment.split("/")
    logger.debug("Processing deployment {}/{}".format(namespace, name))

    for schedule in schedules:
        schedule_expr = schedule.schedule
        logger.debug("{} {}".format(deployment, schedule))

        # if less than 60 seconds have passed from the trigger
        if get_delta_sec(schedule_expr) < 60:
            logger.info("{}Deployment {}/{} matched cron expression '{}'".format(dry_run_prefix(dry_run), namespace, name, schedule_expr))
            scale_deployment(name, namespace, schedule.replicas, dry_run)


def scale_deployment(name: str, namespace: str, replicas: int, dry_run: bool):
    """ Scale the deployment to the given number of replicas """
    try:
        body = dict(spec=dict(replicas=replicas))
        api.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=body, **dry_run_arg(dry_run))
        logger.info("{}Deployment {}/{} scaled to {} replicas".format(dry_run_prefix(dry_run), namespace, name, replicas))
    except Exception as e:
        logger.error("Exception raised while updating deployment {}/{}".format(namespace, name))
        logger.exception(e)


def parse_predefined_schedules(input: str) -> Dict[str, List[RawScalingSchedule]]:
    return {k: [RawScalingSchedule(**i) for i in v] for k, v in json.loads(input).items()}


if __name__ == "__main__":
    logger.info("{}Main loop started".format(dry_run_prefix(DRY_RUN)))

    if DRY_RUN:
        logger.info("* DRY RUN IS ENABLED")

    try:
        predefined_schedules = parse_predefined_schedules(PREDEFINED_SCHEDULES_JSON)
    except ValueError as e:
        predefined_schedules = dict()
        logger.error("could not parse predefined schedules")
        logger.exception(e)

    if len(predefined_schedules.keys()) > 0:
        logger.debug("found predefined schedules")
        logger.debug(json.dumps(predefined_schedules, indent=2))

    while True:
        wait_sec = get_wait_sec()
        logger.debug("Waiting {} seconds until the next minute starts".format(wait_sec))
        sleep(wait_sec)

        logger.debug("Fetching deployments")
        deployments = deployments_to_scale(predefined_schedules).items()

        logger.debug("Processing {} deployments".format(len(deployments)))
        for d, s in deployments:
            process_deployment(d, s, dry_run=DRY_RUN)

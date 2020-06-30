#!/usr/bin/env python3
""" Main module of kube-schedule-scaler """
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from time import sleep

import pykube
from croniter import croniter
from resources import Deployment


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
    datefmt='%d-%m-%Y %H:%M:%S'
)


def get_kube_api():
    """ Initiating the API from Service Account or when running locally from ~/.kube/config """
    try:
        config = pykube.KubeConfig.from_service_account()
    except FileNotFoundError:
        # local testing
        config = pykube.KubeConfig.from_file(
            os.path.expanduser("~/.kube/config"))
    return pykube.HTTPClient(config)


api = get_kube_api()

def schedule_with_annotation_values(schedule, annotations):
    replicas = schedule.get("replicas", None)
    replicas_annotation = annotations.get(replicas, None)
    if replicas_annotation is not None:
        logging.debug("replaced replicas value '{}' with value from annotation: {}".format(replicas, replicas_annotation))
        replicas = replicas_annotation

    min_replicas = schedule.get("minReplicas", None)
    min_replicas_annotation = annotations.get(min_replicas, None)
    if min_replicas_annotation is not None:
        logging.debug("replaced minReplicas value '{}' with value from annotation: {}".format(min_replicas, min_replicas_annotation))
        min_replicas = min_replicas_annotation

    max_replicas = schedule.get("maxReplicas", None)
    max_replicas_annotation = annotations.get(max_replicas, None)
    if max_replicas_annotation is not None:
        logging.debug("replaced maxReplicas value '{}' with value from annotation: {}".format(max_replicas, max_replicas_annotation))
        max_replicas = max_replicas_annotation

    return {**schedule, **dict(replicas=replicas, minReplicas=min_replicas, maxReplicas=max_replicas)}


def deployments_to_scale():
    """ Getting the deployments configured for schedule scaling """
    scaling_dict = {}

    for namespace in list(pykube.Namespace.objects(api)):
        namespace = str(namespace)
        for deployment in Deployment.objects(api).filter(namespace=namespace):
            annotations = deployment.metadata.get("annotations", {})
            f_deployment = str(namespace + "/" + str(deployment))

            schedule_actions = parse_schedules(annotations.get(
                "zalando.org/schedule-actions", "[]"), f_deployment)

            if schedule_actions is None or len(schedule_actions) == 0:
                continue

            # replace annotation pointers with actual values
            scaling_dict[f_deployment] = [schedule_with_annotation_values(schedule, annotations) for schedule in schedule_actions]

    if len(scaling_dict.items()) == 0:
        logging.info("No deployment is configured for schedule scaling")

    return scaling_dict


def parse_schedules(schedules, identifier):
    """ Parse the JSON schedule """
    try:
        return json.loads(schedules)
    except (TypeError, json.decoder.JSONDecodeError) as err:
        logging.error("%s - Error in parsing JSON %s", identifier, schedules)
        logging.exception(err)
        return []


def get_delta_sec(schedule):
    """ Returns the number of seconds passed since last occurence of the given cron expression """
    # get current time
    now = datetime.now()
    # get the last previous occurrence of the cron expression
    time = croniter(schedule, now).get_prev()
    # convert now to unix timestamp
    now = now.replace(tzinfo=timezone.utc).timestamp()
    # return the delta
    return now - time


def get_wait_sec():
    """ Return the number of seconds to wait before the next minute """
    now = datetime.now()
    future = datetime(now.year, now.month, now.day, now.hour, now.minute) + timedelta(minutes=1)
    return (future - now).total_seconds()


def process_deployment(deployment, schedules):
    """ Determine actions to run for the given deployment and list of schedules """
    namespace, name = deployment.split("/")

    for schedule in schedules:
        # when provided, convert the values to int
        replicas = schedule.get("replicas", None)
        if replicas:
            replicas = int(replicas)

        min_replicas = schedule.get("minReplicas", None)
        if min_replicas:
            min_replicas = int(min_replicas)

        max_replicas = schedule.get("maxReplicas", None)
        if max_replicas:
            max_replicas = int(max_replicas)

        schedule_expr = schedule.get("schedule", None)
        logging.debug("%s %s", deployment, schedule)

        # if less than 60 seconds have passed from the trigger
        if get_delta_sec(schedule_expr) < 60:
            # replicas might equal 0 so we check that is not None
            if replicas is not None:
                scale_deployment(name, namespace, replicas)
            # these can't be 0 by definition so checking for existence is enough
            if min_replicas or max_replicas:
                scale_hpa(name, namespace, min_replicas, max_replicas)


def scale_deployment(name, namespace, replicas):
    """ Scale the deployment to the given number of replicas """
    try:
        deployment = Deployment.objects(api).filter(
            namespace=namespace).get(name=name)
    except pykube.exceptions.ObjectDoesNotExist:
        logging.warning("Deployment %s/%s does not exist", namespace, name)
        return

    if replicas is None or replicas == deployment.replicas:
        return
    deployment.replicas = replicas

    try:
        deployment.update()
        logging.info("Deployment %s/%s scaled to %s replicas", namespace, name, replicas)
    except pykube.exceptions.HTTPError as err:
        logging.error("Exception raised while updating deployment %s/%s", namespace, name)
        logging.exception(err)


def scale_hpa(name, namespace, min_replicas, max_replicas):
    """ Adjust hpa min and max number of replicas """
    try:
        hpa = pykube.HorizontalPodAutoscaler.objects(
            api).filter(namespace=namespace).get(name=name)
    except pykube.exceptions.ObjectDoesNotExist:
        logging.warning("HPA %s/%s does not exist", namespace, name)
        return

    # return if no values are provided
    if not min_replicas and not max_replicas:
        return

    # return when both are provided but hpa is already up-to-date
    if (hpa.obj["spec"]["minReplicas"] == min_replicas and
            hpa.obj["spec"]["maxReplicas"] == max_replicas):
        return

    # return when only one of them is provided but hpa is already up-to-date
    if ((not min_replicas and max_replicas == hpa.obj["spec"]["maxReplicas"]) or
            (not max_replicas and min_replicas == hpa.obj["spec"]["minReplicas"])):
        return

    if min_replicas:
        hpa.obj["spec"]["minReplicas"] = min_replicas

    if max_replicas:
        hpa.obj["spec"]["maxReplicas"] = max_replicas

    try:
        hpa.update()
        if min_replicas:
            logging.info("HPA %s/%s minReplicas set to %s", namespace, name, min_replicas)
        if max_replicas:
            logging.info("HPA %s/%s maxReplicas set to %s", namespace, name, max_replicas)
    except pykube.exceptions.HTTPError as err:
        logging.error("Exception raised while updating HPA %s/%s", namespace, name)
        logging.exception(err)


if __name__ == "__main__":
    logging.info("Main loop started")
    while True:
        logging.debug("Waiting until the next minute")
        sleep(get_wait_sec())
        logging.debug("Getting deployments")
        for d, s in deployments_to_scale().items():
            process_deployment(d, s)

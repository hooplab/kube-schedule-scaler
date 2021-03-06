﻿# Kubernetes Schedule Scaler

Kubernetes Schedule Scaler allows you to change the number of running replicas
of a Deployment at specific times. It can be used is to turn on/off
applications that don't need to be always available and reduce cluster resource
utilization, or to adjust the number of replicas when it's known in advance how
the traffic volume is distributed across different time periods.

## Installation

```
$ kubectl apply -f deploy/deployment.yaml
```

## Usage

Just add the annotation to your `Deployment`:

```yaml
  annotations:
    hoopla/scaling.schedule: '[{"schedule": "10 18 * * *", "replicas": "3"}]'
```

The following fields are available:

- `schedule` - cron expression for the schedule
- `replicas` - the number of replicas to scale to

The value of `replicas` can also be a pointer to an annotation value on the same deployment.
The final value will be copied from the annotation. See usage example for annotation pointers below.

### Deployment Example

```yaml
kind: Deployment
metadata:
  name: nginx-deployment
  labels:
    application: nginx-deployment
  annotations:
    some-annotation-value: "3"
    other-annotation-value: "0"
    hoopla/scaling.schedule: |
      [
        {"schedule": "0 7 * * MON-FRI", "replicas": "1"},
        {"schedule": "0 19 * * MON-FRI", "replicas": "other-annotation-value"},
        {"schedule": "0 12 * * MON-FRI", "replicas": "5"},
        {"schedule": "0 16 * * MON-FRI", "replicas": "some-annotation-value"}
      ]
```

## Temporarily disabling scheduled scaling

You can temporarily disable scheduled scaling by adding an annotation `hoopla/scaling.disabled` with the value `"true"`.

```yaml
kind: Deployment
metadata:
  name: nginx-deployment
  labels:
    application: nginx-deployment
  annotations:
    hoopla/scaling.disabled: "true"
    hoopla/scaling.schedule: |
      [
        {"schedule": "0 7 * * MON-FRI", "replicas": "1"},
        {"schedule": "0 19 * * MON-FRI", "replicas": "0"}
      ]
```

## Predefined/shared schedules

Configure predefined schedules by passing the environment variable `PREDEFINED_SCHEDULES` to `schedule_scaling/main.py` with
a json object. E.g.:

```json
{
  "predefined-schedule-a": [
    {"schedule": "0 7 * * MON-FRI", "replicas": "1"},
    {"schedule": "0 19 * * MON-FRI", "replicas": "0"}
  ],
  "predefined-schedule-b": [
    {"schedule": "* 0/2 * * *", "replicas": "1"},
    {"schedule": "* 1/2 * * *", "replicas": "0"}
  ]
}
```

Annotation pointers are still fetched from the deployment, so it's possible to have different `replicas` values for different
deployments even when using predefined schedules.

In the deployment annotation, set the annotation `hoopla/scaling.schedule.predefined` instead of `hoopla/scaling.schedule.predefined`:

```yaml
kind: Deployment
metadata:
  name: nginx-deployment
  labels:
    application: nginx-deployment
  annotations:
    hoopla/scaling.schedule.predefined: predefined-schedule-a
```

If both annotations are present, the predefined schedule takes precedence.

## Logging

You can change the global log level using the `LOG_LEVEL` environment variable (e.g. `LOG_LEVEL=DEBUG`)
and the module log level using the `SCHEDULED_SCALING_LOG_LEVEL` environment variable.

## Dry run

Set the `DRY_RUN` environment variable to `true` to make the kubernetes commands dry run only.

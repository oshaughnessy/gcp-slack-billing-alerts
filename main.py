"""Send GCP billing alerts to Slack by way of a Google Cloud Function.

The notify_slack function is the Google Cloud Functions entry point.
Google Cloud Billing generates events when you set up budgets and alerts:

  https://cloud.google.com/billing/docs/how-to/budgets

Those events are submitted to Google Cloud Pub/Sub, which can then send
the event to Google Cloud Functions. See the architecture here for more details:

  https://cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications

Because of the way Cloud Functions are executed and the cold/warm start
lifecycle (https://cloud.google.com/functions/docs/concepts/exec#cold_starts),
some state information needs to be persistent. State info is kept in a
Google Cloud Secret Manager key to utilize that service as a very simple
key-value store (oh, if only GCP had an equivalent to AWS's SSM Parameter Store;
it's just _so_ convenient). Cloud Datastore or even Cloud Storage may seem like
more attractive options for various reasons, but this is a very limited use of
state -- a single key, the simplicity of using it as a
key-value-store-as-a-service was attractive, and it was an interesting exercise
in using the service.
"""

import base64
import datetime
import json
import logging
import os
import slack
from google.cloud import secretmanager
from mysecret import MySecret

# General references:
# * https://api.slack.com/docs/token-types#bot
# * https://github.com/slackapi/python-slackclient
# * https://github.com/GoogleCloudPlatform/python-docs-samples/blob/master/functions/billing/main.py

# Keep resources in global scope that we want to persist across warm starts of the function.
# See https://cloud.google.com/functions/docs/concepts/exec#cold_starts
SECRET_CLIENT = secretmanager.SecretManagerServiceClient()
SLACK_CLIENT = None


# pylint: disable=too-many-locals
# that could probably be broken up, but there's a fair amount of state and
# message buildup happening in this function that doesn't seem sensible to
# split apart.
def notify_slack(payload, context):
    """Entry point for Cloud Function that receives event data from a Cloud Billing alert.

    Args:
      payload (dict): `attributes` and `data` keys. See
        https://cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications#notification_format
      context (google.cloud.functions.Context): event metadata. See
        https://cloud.google.com/functions/docs/writing/background#function_parameters
    """

    # pylint: disable=global-statement
    # we're taking advantage of Google Cloud Function cold/warm starts
    global SLACK_CLIENT

    # payload metadata comes in `attributes`, actual event message comes in `data`
    alert_attrs = payload.get("attributes")
    alert_data = json.loads(base64.b64decode(payload.get("data")).decode("utf-8"))
    logging.info(
        "new billing alert; context=%s, attributes=%s, data=%s",
        context,
        alert_attrs,
        alert_data,
    )

    # parse the GCP resource name to extract information about where we're running
    resource_name = context.resource.get("name").split("/")
    if resource_name[0] == "projects":
        project_id = resource_name[1]
    else:
        project_id = "UNKNOWN"
    if resource_name[2] == "topics":
        topic_id = resource_name[3]
    else:
        topic_id = "UNKNOWN"
    logging.debug(
        "extracted resource info from context: project_id=%s, topic_id=%s",
        project_id,
        topic_id,
    )

    # we (re)store state info in a Google Cloud Secret because it's already
    # being used for our Slack token
    billing_id = alert_attrs.get("billingAccountId")
    budget_id = alert_attrs.get("budgetId")
    secret = MySecret(
        project_id,
        context={
            "billing_id": billing_id,
            "budget_id": budget_id,
            "topic_id": topic_id,
        },
        secret_client=SECRET_CLIENT,
    )
    alert_state = restore_state(secret)

    # extract relevant info from the alert data for our Slack message
    budget_name = alert_data.get("budgetDisplayName")
    cost = "${:,.2f}".format(float(alert_data.get("costAmount")))
    budget = "${:,.2f}".format(float(alert_data.get("budgetAmount")))
    currency = alert_data.get("currencyCode")
    interval = datetime.datetime.strptime(
        alert_data.get("costIntervalStart"), "%Y-%m-%dT%H:%M:%S%z"
    )
    interval_str = interval.strftime("%b %d, %Y")
    threshold = float(alert_data.get("alertThresholdExceeded")) * 100

    # Compose our Slack alert
    # https://api.slack.com/reference/surfaces/formatting#basics
    slack_msg = (
        f":gcp: _{budget_name}_ billing alert :money_with_wings:\n"
        f"*{cost}* is over {threshold}% of budgeted {budget} {currency} "
        f"for period starting {interval_str}"
    )
    if threshold > 100:
        slack_msg += ":sad: https://media.giphy.com/media/l0HFkA6omUyjVYqw8/giphy.gif"

    # Unlike email alerts, Google Cloud Billing's _programmatic_ alerts repeat
    # as long as the alert is valid, so we need to self-throttle.
    # This is the whole reason we need to keep state.

    # if we're dealing with a new interval, reset our state
    last_interval = alert_state.get("last_interval", datetime.datetime.fromordinal(1))
    if interval != last_interval:
        logging.debug(
            "%s/%s: last interval @ %s != new @ %s: resetting alert state",
            billing_id,
            budget_id,
            last_interval,
            interval,
        )
        alert_state["last_interval"] = interval
        alert_state["last_threshold"] = -1

    # only send an alert for the given context if we haven't already done so
    last_threshold = alert_state.get("last_threshold", -1)
    if threshold <= last_threshold:
        logging.info("%s/%s: ignoring repeat alert...", billing_id, budget_id)
        logging.debug(
            "last_interval=%s, last_threshold=%s, msg=%s",
            last_interval,
            last_threshold,
            slack_msg,
        )
        return
    logging.info(
        "%s/%s: alert came for new threshold: %s", billing_id, budget_id, threshold
    )
    alert_state["last_threshold"] = threshold

    save_state(secret, alert_state)

    # finally, send our message to Slack
    logging.info(
        "last_interval=%s, last_threshold=%s, msg=%s",
        last_interval,
        last_threshold,
        slack_msg,
    )
    if not SLACK_CLIENT:
        SLACK_CLIENT = slack_connect(project_id, SECRET_CLIENT)

    channel = os.environ.get("SLACK_CHANNEL", "#gcp-test")
    slack_post(SLACK_CLIENT, channel, slack_msg)


def restore_state(secret):
    """Restore our alert state from a Secret.

    Args:
        secret (MySecret object): has data with secret info and manages access
                                  to Google Cloud Secret Manager

    Returns:
        dict with state info
    """

    logging.debug("restoring state from secret")
    if secret.data:
        state = secret.data
    else:
        state = dict()
    return state


def save_state(secret, state):
    """Save our alert state in a Secret so we can pull it again the next time we run."""

    secret.data = state


def slack_connect(project_id, secret_client):
    """Connect to Slack and return a client.

    Args:
        project_id (str): GCP project identifier

    Returns:
        slack client object
    """

    client = None

    # Try reading a token from the environment first
    token = os.environ.get("SLACK_API_TOKEN", None)

    # If that didn't work, get it from Google Secret Manager
    if not token:
        secret_name = "gcp-slack-notifier-SLACK_API_TOKEN"
        secret_path = secret_client.secret_version_path(
            project_id, secret_name, "latest"
        )
        secret_version = secret_client.access_secret_version(secret_path)
        token = secret_version.payload.data.decode("utf-8").strip()

    if not token:
        logging.error("no Slack API token available, aborting")
        return None

    try:
        # log the token type (the 1st 4 chars) and the very end -- not enough to steal it,
        # but enough to identify which token is in use when debugging access & scopes
        logging.debug("connecting to slack; token=%s...%s", token[:4], token[-4:])
        client = slack.WebClient(token=token)
    except slack.errors.SlackApiError as err:
        logging.error(err)
    return client


def slack_post(client, channel, msg):
    """Post a message to a Slack channel.

    May require these Oauth Bot Token scopes:
        channels:join:i      Join public channels in the workspace
        chat:write:          Send messages as bot user
        chat:write.customize Send messages as defined bot user with a customized
                             username and avatar
        chat:write.public    Send messages to channels bot isn't a member of
        users:write          Set presence
    See https://api.slack.com/authentication/token-types#bot for more.

    Args:
        client (slack.WebClient): previously-connected Slack web client
        channel (str):            location in Slack where message should be posted
        msg (str):                message to send

    Returns:
        None
    """

    try:
        logging.debug("posting to slack; msg=%d chars", len(msg))
        client.chat_postMessage(channel=channel, text=msg)
    except slack.errors.SlackApiError as err:
        logging.error(err)

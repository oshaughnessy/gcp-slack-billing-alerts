# Receive Google Cloud Billing alerts in Slack

This code lets you send GCP billing alerts to Slack by way of a
Google Cloud Function.

Google Cloud Billing generates events when you set up budgets and alerts:

  https://cloud.google.com/billing/docs/how-to/budgets

Those events are submitted to Google Cloud Pub/Sub, which can then send
the event to Google Cloud Functions. See the architecture here for more details:

  https://cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications

The `main:notify_slack` function is the Google Cloud Functions entry point.

## Setup

Before your budget alerts function can be deployed, a handful of things must be
created or configured.

### First

Ensure you have a project for your code created in Google Cloud Platform.

### Second

Next, if you want to automatically deploy from GitHub to Google
Cloud Platform, define [GitHub Secrets](https://help.github.com/en/actions/configuring-and-managing-workflows/creating-and-storing-encrypted-secrets) in your repository for the following variables:

* CLOUDSDK_CORE_PROJECT
* CLOUDSDK_COMPUTE_REGION
* CLOUDSDK_COMPUTE_ZONE
* CLOUDSDK_SERVICE_ACCOUNT:
* SLACK_API_TOKEN: Oauth token for a Slack bot. See https://api.slack.com/docs/token-types#bot

### Third

Define the same variables in a local copy for your dev environment.
Make sure that version is included in `.gitignore` (`Makefile.dev.env`
is listed there already) so you don't push it back to your repo.
It might be fine if your repo is private -- it won't contain secrets,
but you shouldn't make it public, either.

    cp Makefile.env.sample Makefile.dev.env
    vim Makefile.dev.env

### Fourth: Service Account

Create a service account in GCP:

    gcloud auth login
    make service-account

### Fifth: Service Key

Create a service key for the service account you just created.

    make service-key

Put that key into a GitHub secret in your repository. Call the secret
`GOOGLE_APPLICATION_CREDENTIALS_JSON`:

    cat gcp-slack-notifier.key

## A note on keeping state

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

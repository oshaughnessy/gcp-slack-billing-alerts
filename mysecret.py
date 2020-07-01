"""Provide a class to manage Google Cloud Secret Manager secrets
"""

import logging
import pickle
import parse
import google

# See: https://googleapis.dev/python/secretmanager/latest/gapic/v1/api.html
class MySecret:
    """Manage a secret string in Google Cloud Secret Manager

    Attributes:
        client: google.cloud.secretmanager client
        secret: Secret Manager object
        name: full key name of Secret Manager object, including the project path
        project_id: Google Cloud Platform project in which the secret is stored
        parent: first path of path to Secret Manager object, before the relative name
        relative_name: short name of Secret Manager object, after "projects/_ID_/secrets/"
    """

    client = None
    relative_name = None
    parent = None
    project_id = None
    secret = None

    def __init__(self, project_id, name=None, context=None, secret_client=None):
        """Creates a new MySecret object and prepares it for use.

        Args:
            project_id (str): Google Cloud Platform project identifier
            name (str): Relative name of the secret key (optional)
            context (dict): Info used to derive a relative name (optional);
                relative name will be built from a handful of keys if
                included so that the full string looks may like this:
                "gcp-slack-notifier-state_{topic_id}_BILLING-{billing_id}_BUDGET-{budget_id}"
            secret_client (obj): active google.cloud.secretmanager client
        """
        self._data = None
        self.client = secret_client
        self.parent = secret_client.project_path(project_id)
        self.project_id = project_id
        context = context or {}
        billing_id = context.get("billing_id", None)
        budget_id = context.get("budget_id", None)
        topic_id = context.get("topic_id", None)

        if name:
            self.relative_name = name
        else:
            self.relative_name = "gcp-slack-notifier-state"
            if topic_id:
                self.relative_name += f"_{topic_id}"
            if billing_id:
                self.relative_name += f"_BILLING-{billing_id}"
            if budget_id:
                self.relative_name += f"_BUDGET-{budget_id}"

        all_secrets = secret_client.list_secrets(self.parent)
        for found_secret in all_secrets:
            found_parts = parse.parse(
                "projects/{self.project_id}/secrets/{relative_name}", found_secret.name
            )
            if found_parts.named["relative_name"] == self.relative_name:
                logging.debug("found existing secret: %s", found_secret.name)
                self.secret = found_secret
                return

        logging.info("creating new secret: %s/%s", self.parent, self.relative_name)
        self.secret = secret_client.create_secret(
            self.parent, self.relative_name, {"replication": {"automatic": {},},}
        )

    def __repr__(self):
        return self.secret.name

    @property
    def data(self):
        """Get the latest version of the secret info.

        If a cached copy of the info is available, returns that.
        If not, it pulls the latest from Google

        Returns:
            None if unavailable, otherwise
            Python data type as restored from Google Secret & unpickled
        """

        if not self._data:
            try:
                logging.debug("refreshing latest data for %s", self.secret.name)
                secret_version = self.client.access_secret_version(
                    self.secret.name + "/versions/latest"
                )
                self._data = pickle.loads(secret_version.payload.data)
            except google.api_core.exceptions.GoogleAPICallError as err:
                logging.warning(
                    "error reading %s/versions/latest (may just not exist)",
                    self.secret.name,
                )
                logging.warning(err)
        return self._data

    @data.setter
    def data(self, value):
        """Create a new version of the Secret and update our cached copy.

        Args:
            value: Converts the python data type to a pickled, binary object.
        Returns:
            new Secret version
        """

        logging.debug("adding new version of %s: %s", self.secret.name, value)
        version = self.client.add_secret_version(
            self.secret.name, {"data": pickle.dumps(value)}
        )
        self._data = value
        return version

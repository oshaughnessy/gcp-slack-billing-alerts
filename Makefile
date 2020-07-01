include Makefile*.env

# set some defaults in case Makefile*.env didn't set them
ifndef GOOGLE_PROJECT_NUMBER
GOOGLE_PROJECT_NUMBER := $(shell gcloud projects describe $(CLOUDSDK_CORE_PROJECT) --format 'value(projectNumber)')
endif
ifndef API_SECRET_PATH
API_SECRET_PATH := projects/$(GOOGLE_PROJECT_NUMBER)/secrets/gcp-slack-notifier-SLACK_API_TOKEN
endif
ifndef CLOUD_FUNCTION
CLOUD_FUNCTION := slack-budget-notification
endif
ifndef PUBSUB_TOPIC
PUBSUB_TOPIC := billing-alerts
endif
ifndef SLACK_CHANNEL
SLACK_CHANNEL := "\#google-cloud"
endif
ifndef
LOGS := 15
endif
PYTHON_RUNTIME_VER=python37

export PIP_REQUIRE_VIRTUALENV=false
export PIPENV_VENV_IN_PROJECT=true
export PIPENV_IGNORE_VIRTUALENVS=true
export PATH:=$(shell python3 -m site --user-base)/bin:$(PATH)

ROOT_PATH=$(PWD)
SRC_PATH=$(ROOT_PATH)
BUILD_PATH=$(ROOT_PATH)/build
BUILD_DOCS_PATH=build/docs
TESTS_PATH=$(ROOT_PATH)/tests
#PYTHON_SRC=$(wildcard *.py)
PYTHON_SRC=main.py
COVERAGE_MIN ?= 75
PYLINT=pipenv run pylint
PYLINT_OPTIONS=
REPORT_PYLINT=$(BUILD_DOCS_PATH)/pylint.log

help:
	@echo "targets: deploy, install, pubsub-test, function-test, local, log"
	@echo "Slack Billing Alerts in GCP"
	@echo "==========================="
	@echo ""
	@echo "initial setup:"
	@echo "    show: show relevant config info (handy before doing anything else)"
	@echo "    permissions-info: describe permissions needed to run the function"
	@echo "    install: do initial setup and deploy the function to GCP"
	@echo "    - slack-secret: use during initial setup to provision a GCP Secret Manager secret"
	@echo "      with your Slack API token (paste it in when prompted)"
	@echo "    - service-account: create a service account for provisioning"
	@echo "    - service-account-roles: grant required roles to the service account"
	@echo "    - service-key: create a service key from the service account"
	@echo "    - deploy: launch or update the container deployed to Google Cloud Functions"
	@echo "    python-reqs: install pipenv, install python requirements"
	@echo ""
	@echo "google cloud targets:"
	@echo "    gcloud-apis: ensure all GCP APIs necessary for deployment are enabled"
	@echo "    log: print the most recent $(LOGS) log messages from the Google Cloud Function"
	@echo "        (override # lines by setting LOGS)"
	@echo "    watch: tail Google Cloud Function logs in a full-terminal view"
	@echo "        (override # lines by setting LOGS)"
	@echo "    pubsub-test: invoke the Cloud Function by passing an event to the pub-sub topic"
	@echo "        that feeds it"
	@echo "    function-test: invoke the Cloud Function directly"
	@echo ""
	@echo "local dev targets:"
	@echo "    code-test: reformat Python code with Black (https://github.com/psf/black)"
	@echo "    - pylint: check code for linter warnings and errors"
	@echo "    - safety: check code for known vulnerabilities in dependencies"
	@echo "    - test-unit: run unit tests on the Python code"
	@echo "    format: reformat Python code with Black (https://github.com/psf/black)"
	@echo "    local: run the Python function locally"
	@echo "    update-dependencies: rebuild Pipfile.lock and requirements.txt after updating Pipfile"

show: printenv gcloud-info

printenv:
	@printf "\n## Relevant environmental variables\n\n"
	@env |egrep '^(CLOUDSDK|GOOGLE|PATH|PIP|SLACK)' |sort

gcloud-info:
	@printf "\n## Google Cloud SDK config\n\n"
	@gcloud config list

#
# Google Cloud SDK targets
#

gcloud-auth:
	gcloud auth activate-service-account "$(CLOUDSDK_SERVICE_ACCOUNT)" --key-file "$(GOOGLE_APPLICATION_CREDENTIALS)"

service-key: $(GOOGLE_APPLICATION_CREDENTIALS)

install: slack-secret service-account service-account-roles service-key deploy
	@echo "Cloud Function $(CLOUD_FUNCTION) and dependencies installed"

$(GOOGLE_APPLICATION_CREDENTIALS):
	printf "\n## Generating new service key for $(CLOUDSDK_SERVICE_ACCOUNT)\n\n"; \
	tmpfile=$$(mktemp); \
	env GOOGLE_APPLICATION_CREDENTIALS= gcloud iam service-accounts keys create $$tmpfile --iam-account $(CLOUDSDK_SERVICE_ACCOUNT); \
	mv -v $$tmpfile $(GOOGLE_APPLICATION_CREDENTIALS)

gcloud-apis:
	@printf "\n## Enabling Google Cloud APIs for Cloud Billing budget notifications\n\n"
	@gcloud config list
	@gcloud services enable billingbudgets.googleapis.com \
	                        cloudfunctions.googleapis.com \
	                        cloudresourcemanager.googleapis.com \
	                        secretmanager.googleapis.com
	@gcloud services list

# IAM permissions for Secret Manager:
# https://cloud.google.com/secret-manager/docs/access-control
permissions-info:
	@echo "Create service account:"
	@echo ""
	@echo "    gcloud iam service-accounts create $(CLOUDSDK_SERVICE_ACCOUNT)"
	@echo ""
	@echo "Grant roles/secretmanager.admin permission to your service account."
	@echo "It's needed to create $(API_SECRET_PATH)"
	@echo "as well as a state key for each billing+budget ID that will use the function."
	@echo ""
	@echo "    gcloud projects add-iam-policy-binding $(CLOUDSDK_CORE_PROJECT) --member serviceAccount:$(CLOUDSDK_SERVICE_ACCOUNT) --role "roles/secretmanager.admin""
	@echo ""
	@echo "    see: 'make service-account'"
	@echo ""
	@echo "For local development, create a key for your service account, download it,"
	@echo "and put the path in a local environmental variable, GOOGLE_APPLICATION_CREDENTIALS."
	@echo ""
	@echo "    gcloud iam service-accounts keys create $(GOOGLE_APPLICATION_CREDENTIALS) --iam-account $(CLOUDSDK_SERVICE_ACCOUNT)"
	@echo ""
	@echo "    see: 'make service-key'"
	@echo ""
	@echo "Also consider installing functions-framework-python:"
	@echo "    https://github.com/GoogleCloudPlatform/functions-framework-python"

service-account:
	@printf "\n## Creating GCP service account $(CLOUDSDK_SERVICE_ACCOUNT)\n\n"
	@acct_shortname=$$(echo "$(CLOUDSDK_SERVICE_ACCOUNT)" |cut -d@ -f1); \
	 gcloud iam service-accounts describe $(CLOUDSDK_SERVICE_ACCOUNT) \
	 || (echo creating $$acct_shortname; gcloud iam service-accounts create $$acct_shortname)

# see: https://cloud.google.com/functions/docs/reference/iam/roles
service-account-roles:
	@printf "\n## Binding GCP service account $(CLOUDSDK_SERVICE_ACCOUNT)\n\n"
	@printf "\n### Granting secretmanager.admin\n\n"
	@gcloud projects add-iam-policy-binding $(CLOUDSDK_CORE_PROJECT) \
	 --member serviceAccount:$(CLOUDSDK_SERVICE_ACCOUNT) --role roles/secretmanager.admin
	@printf "\n### Granting cloudfunctions.admin\n\n"
	@gcloud projects add-iam-policy-binding $(CLOUDSDK_CORE_PROJECT) \
	 --member serviceAccount:$(CLOUDSDK_SERVICE_ACCOUNT) --role roles/cloudfunctions.admin
	@printf "\n### Granting iam.serviceAccountUser\n\n"
	@gcloud projects add-iam-policy-binding $(CLOUDSDK_CORE_PROJECT) \
	 --member serviceAccount:$(CLOUDSDK_SERVICE_ACCOUNT) --role roles/iam.serviceAccountUser

slack-secret:
	@echo "creating a Cloud Secret for your Slack API token at $(API_SECRET_PATH)"
	gcloud secrets create $(API_SECRET_PATH) --replication-policy=automatic || true
	gcloud secrets versions add $(API_SECRET_PATH) --data-file=-

# see: https://cloud.google.com/functions/docs/deploying/filesystem
# FIX: the function should ideally use a service account (deploy ... --service-account)
# that has a more limited scope than the $CLOUDSDK_SERVICE_ACCOUNT we're using
# to deploy the function itself
deploy:
	gcloud functions deploy $(CLOUD_FUNCTION) --trigger-topic=$(PUBSUB_TOPIC) --set-env-vars=SLACK_CHANNEL=$(SLACK_CHANNEL) --runtime=$(PYTHON_RUNTIME_VER) --entry-point=notify_slack
	@echo "Cloud Function $(CLOUD_FUNCTION) deployed"

#
# Dev & debugging targets
#

python-reqs:
	@printf "\n## Installing Python project requirements\n\n"
	if [ "$$CI" ]; then \
	    sudo -H pip3 install pipenv; \
	else \
	    pip3 install --user pipenv; \
	fi
	@pipenv install

local:
	pipenv run functions-framework --target=notify_slack --debug

log:
	gcloud functions logs read $(CLOUD_FUNCTION) --limit $(LOGS)

watch:
	watch -n 10 gcloud functions logs read $(CLOUD_FUNCTION) --limit 7

pubsub-test:
	gcloud pubsub topics publish $(PUBSUB_TOPIC) --message "$$(<billing-event.json)"

function-test:
	gcloud functions call $(CLOUD_FUNCTION) --data "$$(<function-data.json)"

test-unit:
	@if [ -d $(TESTS_PATH)/unit ]; then \
	     export PYTHONPATH=$(SRC_PATH); \
	     pytest \
	     --cov=$(SRC_PATH) \
	     --cov-report term-missing \
	     --cov-fail-under=$(COVERAGE_MIN) $(TESTS_PATH) \
	     || (echo "Unit tests failed!"; exit 1) \
	fi

code-test: pylint test-unit safety

format:
	@if [ -d $(SRC_PATH) ]; then \
	     echo "Analyzing code formatting ..."; \
	     pipenv run black -v $(SRC_PATH) \
	     || (echo "Code formatting failed!"; exit 1) \
	fi

pylint:
	@if [ -d $(SRC_PATH) ]; then \
	    echo "Analyzing code linting ..."; \
	    mkdir -p $(BUILD_DOCS_PATH); \
	    export PYTHONPATH=$(SRC_PATH); \
	    test -e $(PWD)/.pylintrc || $(PYLINT) --generate-rcfile > $(PWD)/.pylintrc; \
	    $(PYLINT) $(PYLINT_OPTIONS) $(PYTHON_SRC) > $(REPORT_PYLINT) || (cat $(REPORT_PYLINT); exit 1); \
	fi

safety: requirements.txt
	@if [ -d $(SRC_PATH) ]; then \
	    echo "Checking requirements for security vulnerabilities..."; \
	    pipenv run python -m safety check -r requirements.txt --full-report \
	    || (echo "Security check failed!"; exit 1) \
	fi

requirements.txt: Pipfile.lock
	@echo "Updating requirements.txt from Pipfile"
	@pipenv lock -r >requirements.txt

update-dependencies: Pipfile requirements.txt
	@echo "Updating Pipfile.lock"
	@pipenv update

clean:
	find . -iname '*.pyc' -delete
	rm -rf $(BUILD_PATH)
	rm -f .coverage
	rm -rf .venv


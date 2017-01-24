""" Run all tests for openshift-tools repository """
# This script expects a single environment variable to be defined with a myriad of json data:
#    GITHUB_WEBHOOK_PAYLOAD
#
# The data expected from this payload is that generated by the pull reqeust edit webhook,
# defined here:
#    https://developer.github.com/v3/activity/events/types/#pullrequestevent
#
# The script will parse the JSON and define a list of environment variables for consumption by
# the validation scripts. Then, each *.py file in ./validators/ (minus specified exclusions)
# will be run. The list of variables defined is below:
#  Github stuff
#    PRV_TITLE         Title of the pull request
#    PRV_BODY          Description of the pull request
#    PRV_PULL_ID       ID of the pull request
#    PRV_PULL_URL      URL of the pull request
#
#  Base info
#    PRV_BASE_SHA      SHA of the target being merged into
#    PRV_BASE_REF      ref (usually branch name) of the base
#    PRV_BASE_LABEL    Base label
#    PRV_BASE_NAME     Full name of the base 'namespace/reponame'
#
#  Remote (or "head") info
#    PRV_REMOTE_SHA    SHA of the branch being merged
#    PRV_REMOTE_REF    ref (usually branch name) of the remote
#    PRV_REMOTE_LABEL  Remote label
#    PRV_REMOTE_NAME   Full name of the remote 'namespace/reponame'
#    PRV_CURRENT_SHA   The SHA of the merge commit
#
#  Other info
#    PRV_CHANGED_FILES List of files changed in a pull request
#

import os
import json
import subprocess
import sys
import requests

import github_helpers

EXCLUDES = [
    "common.py",
    ".pylintrc"
]
# The relative path to the testing validator scripts
VALIDATOR_PATH = "jenkins/test/validators/"
# The string to accept in PR comments to initiate testing by a whitelisted user
TEST_STRING = "[test]"

def run_cli_cmd(cmd, exit_on_fail=True):
    '''Run a command and return its output'''
    proc = subprocess.Popen(cmd, bufsize=-1, stderr=subprocess.PIPE, stdout=subprocess.PIPE,
                            shell=False)
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        print "Unable to run " + " ".join(cmd) + " due to error: " + stderr
        if exit_on_fail:
            sys.exit(proc.returncode)
        else:
            return False, stdout
    else:
        return True, stdout

def assign_env(pull_request):
    '''Assign environment variables base don github webhook payload json data'''
    # Github environment variables
    os.environ["PRV_TITLE"] = pull_request["title"]
    # Handle pull request body in case it is empty
    os.environ["PRV_BODY"] = (pull_request["body"] if pull_request["body"] is not None else "")
    os.environ["PRV_PULL_ID"] = pull_request["number"]
    os.environ["PRV_URL"] = pull_request["url"]

    # Base environment variables
    base = pull_request["base"]
    os.environ["PRV_BASE_SHA"] = base["sha"]
    os.environ["PRV_BASE_REF"] = base["ref"]
    os.environ["PRV_BASE_LABEL"] = base["label"]
    os.environ["PRV_BASE_NAME"] = base["repo"]["full_name"]

    # Remote environment variables
    head = pull_request["head"]
    os.environ["PRV_REMOTE_SHA"] = head["sha"]
    os.environ["PRV_REMOTE_REF"] = head["ref"]
    os.environ["PRV_REMOTE_LABEL"] = head["label"]
    os.environ["PRV_REMOTE_NAME"] = head["repo"]["full_name"]

    # Other helpful environment variables
    baserepo = base["repo"]["full_name"]
    prnum = pull_request["number"]
    changed_files = github_helpers.get_changed_files(baserepo, prnum)
    os.environ["PRV_CHANGED_FILES"] = ",".join(changed_files)

def merge_changes(pull_request):
    """ Merge changes into current repository """
    pull_id = pull_request["number"]

    run_cli_cmd(['/usr/bin/git', 'fetch', "--tags", "origin", "+refs/head/*:refs/remotes/origin/*",
                 "+refs/pull/*:refs/remotes/origin/pr/*"])
    _, output = run_cli_cmd(['/usr/bin/git', 'rev-parse',
                             'refs/remotes/origin/pr/'+pull_id+'/merge^{commit}'])
    current_rev = output.rstrip()
    run_cli_cmd(['/usr/bin/git', 'config', 'core.sparsecheckout'], exit_on_fail=False)
    run_cli_cmd(['/usr/bin/git', 'checkout', '-f', current_rev])
    os.environ["PRV_CURRENT_SHA"] = current_rev

def run_validators():
    """ Run all test validators """
    # First, add the validator direcotry to the python path to allow
    # modules to be loaded by pylint
    pypath = os.getenv("PYTHONPATH", "")
    if pypath != "":
        os.environ["PYTHONPATH"] = VALIDATOR_PATH + os.pathsep + pypath
    else:
        os.environ["PYTHONPATH"] = VALIDATOR_PATH

    failure_occured = False
    validators = [validator for validator in os.listdir(VALIDATOR_PATH) if
                  os.path.isfile(os.path.join(VALIDATOR_PATH, validator))]
    for validator in validators:
        skip = False
        for exclude in EXCLUDES:
            if validator == exclude:
                skip = True
        if skip:
            continue
        validator_abs = os.path.join(VALIDATOR_PATH, validator)
        executer = ""
        _, ext = os.path.splitext(validator)
        if ext == ".py":
            executer = "/usr/bin/python"
        elif ext == ".sh":
            executer = "/bin/sh"
        # If the ext is not recongized, try to just run the file
        print "Executing validator: " + executer + " " + validator_abs
        success, output = run_cli_cmd([executer, validator_abs], exit_on_fail=False)
        print output
        if not success:
            print validator + " failed!"
            failure_occured = True

    if failure_occured:
        return False
    return True

# Check both the user and org whitelist for the user in this payload
# Additionally, if the payload is an issue_comment, check to ensure that the
# TEST_STRING is included in the comment.
def pre_test_check(payload):
    """ Get and check the user whitelist for testing from mounted secret volume """
    # Get user from payload
    user = ""
    comment_made = False
    if "pull_request" in payload:
        user = payload["pull_request"]["user"]["login"]
    elif "comment" in payload:
        user = payload["comment"]["user"]["login"]
        comment_made = True
    else:
        print "Webhook payload does not include pull request user or issue comment user data"
        sys.exit(1)

    if comment_made:
        body = payload["comment"]["body"]
        if not "[test]" in body.split(" "):
            print "Pull request coment does not include test string \"" + TEST_STRING +"\""
            # Exit success here so that the jenkins job is marked as  a success,
            # since no actual error occurred, the expected has happened
            sys.exit(0)

    # Get secret information from env variable
    secret_dir = os.getenv("WHITELIST_SECRET_DIR")
    if secret_dir == "":
        print "ERROR: $WHITELIST_SECRET_DIR undefined. This variable should exist and" + \
            " should point to the mounted volume containing the admin whitelist"
        sys.exit(2)
    # Extract whitelist from secret volume
    user_whitelist_file = open(os.path.join("/", secret_dir, "users"), "r")
    user_whitelist = user_whitelist_file.read()
    user_whitelist_file.close()
    if user_whitelist == "" or user not in user_whitelist.split(","):
        if not check_org_whitelist(user, secret_dir):
            print "WARN: User " + user + " not in admin or org whitelist."
            # exit success here so that the jenkins job is marked as a success,
            # since no actual error occured, the expected has happened
            sys.exit(0)

# Get the members of each organization in the organization whitelist for the user. If
# the user is a member of any of these organizations, return True
def check_org_whitelist(user, secret_dir):
    """ Determine whether user is a member of any org in the org whitelist """
    org_whitelist_file = open(os.path.join("/", secret_dir, "orgs"), "r")
    org_whitelist = org_whitelist_file.read()
    org_whitelist_file.close()
    for org in org_whitelist.split(","):
        if github_helpers.org_includes(user, org):
            return True
    return False

# The payload may be for an issue_comment or for a pull_request. This method determines
# which of those this payload represents. If the payload is an issue_comment, the
# relevant pull_request information is gathered from Github
def get_pull_request_info(payload):
    """ Get the relevant pull request details for this webhook payload """
    if "pull_request" in payload:
        return payload["pull_request"]

    if not "issue" in payload:
        print "Webhook payload does not include pull request or issue data"
        sys.exit(1)
    if not "pull_request" in payload["issue"]:
        print "Webhook payload is for an issue comment, not pull request."
        sys.exit(1)

    pull_request_url = payload["issue"]["pull_request"]["url"]
    response = requests.get(pull_request_url)
    response.raise_for_status()
    pull_request_json = response.text
    try:
        pull_request = json.loads(pull_request_json, parse_int=str, parse_float=str)
    except ValueError as error:
        print "Unable to load JSON data from " + pull_request_url
        print error
        sys.exit(1)
    return pull_request

def main():
    """ Get the payload, merge changes, assign env, and run validators """
    # Get the github webhook payload json from the defined env variable
    payload_json = os.getenv("GITHUB_WEBHOOK_PAYLOAD", "")
    if payload_json == "":
        print 'No JSON data provided in $GITHUB_WEBHOOK_PAYLOAD'
        sys.exit(1)
    try:
        payload = json.loads(payload_json, parse_int=str, parse_float=str)
    except ValueError as error:
        print "Unable to load JSON data from $GITHUB_WEBHOOK_PAYLOAD:"
        print error
        sys.exit(1)

    # Run several checks to ensure tests should be run for this payload
    pre_test_check(payload)

    # Extract or get the pull request information from the payload
    pull_request = get_pull_request_info(payload)

    remote_sha = pull_request["head"]["sha"]
    pull_id = pull_request["number"]
    repo = pull_request["base"]["repo"]["full_name"]

    # Update the PR to inform users that testing is in progress
    github_helpers.submit_pr_status_update("pending", "Automated tests in progress",
                                           remote_sha, repo)

    # Merge changes from pull request
    merge_changes(pull_request)

    # Assign env variables for validators
    assign_env(pull_request)

    # Run validators
    success = run_validators()

    # Determine and post result of tests
    if not success:
        github_helpers.submit_pr_comment("Tests failed!", pull_id, repo)
        github_helpers.submit_pr_status_update("failure", "Automated tests failed",
                                               remote_sha, repo)
        sys.exit(1)
    github_helpers.submit_pr_comment("Tests passed!", pull_id, repo)
    github_helpers.submit_pr_status_update("success", "Automated tests passed",
                                           remote_sha, repo)

if __name__ == '__main__':
    main()

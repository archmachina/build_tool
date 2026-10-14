
import argparse
import os
import shlex
import subprocess
import sys
import logging
import re
import requests
import textwrap
import json

# Exceptions
class ArgumentException(Exception):
    pass

class CommandException(Exception):
    pass

class DeployError(Exception):
    pass

class VersionParseError(Exception):
    pass

class BuildFailure(Exception):
    pass

# Validation
def val_arg(val, message):
    if not val:
        raise ArgumentException(message)


# Logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def configure_logging(debug=False):

    level = logging.INFO
    if debug:
        level = logging.DEBUG

    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", force=True)

# Actions decorator
actions = {}

def action(func):
    actions[func.__name__] = func

    return func

# Shortform functions for particular shells or invocation types
def bash(cmd, **kwargs):
    return exec_cmd(cmd, interpreter="bash", **kwargs)

def pwsh(cmd, **kwargs):
    return exec_cmd(cmd, interpreter="pwsh -noni -c -", **kwargs)

def shell(cmd, **kwargs):
    return exec_cmd(cmd, shell=True, **kwargs)

class ExecStatus:
    def __init__(self):
        self.returncode = None
        self.stdout = None

# Exec to actually run processes
def exec_cmd(cmd, *, shell=False, env=None, interpreter=None, capture=False, allowed_rc=None, ignore_rc=False):

    # Command
    val_arg(isinstance(cmd, (list, str)), "Invalid type for cmd. Must be a list or string")
    if isinstance(cmd, list):
        val_arg(all([isinstance(x, str) for x in cmd]), "Invalid value for cmd. Some members are not strings")

    # Shell
    val_arg(isinstance(shell, bool), "Invalid value for shell argument")

    # Capture - Whether to capture the output of the command
    val_arg(isinstance(capture, bool), "Invalid type for capture. Must be bool")

    # Interpreter
    val_arg(isinstance(interpreter, (str, type(None))), "Invalid interpreter. Must be str or None")

    # Environment variables
    val_arg(isinstance(env, (dict, type(None))), "Invalid type for env. Must be dict or None")

    envs = os.environ.copy()

    if isinstance(env, dict):
        val_arg(all([isinstance(x, str) for x in env.keys()]), "env must be a dict str to str")
        val_arg(all([isinstance(x, str) for x in env]), "env must be a dict str to str")

        envs.update(env)

    # allowed_rc
    val_arg(isinstance(allowed_rc, (list, type(None))), "Invalid type for allowed_rc. Must be list or None")

    if allowed_rc is None:
        allowed_rc = [0]

    if isinstance(allowed_rc, list):
        val_arg(all([isinstance(x, int) for x in allowed_rc]), "Invalid values in allowed_rc. Must be int")

    # ignore_rc
    val_arg(isinstance(ignore_rc, bool), "Invalid type for ignore_rc. Must be bool")

    # Arguments to subprocess.run
    subprocess_args = {
        "env": envs,
        "stdout": None,
        "stderr": subprocess.STDOUT,
        "shell": shell,
        "text": True,
    }

    # If we're capturing, stdout should come back via pipe
    if capture:
        subprocess_args["stdout"] = subprocess.PIPE

    # If an interpreter is defined, this is the executable to call instead
    if interpreter is not None and interpreter != "":
        call_args = interpreter
        subprocess_args["input"] = cmd
    else:
        call_args = cmd
        subprocess_args["stdin"] = subprocess.DEVNULL

    # If we're not using shell interpretation, then split the command in to
    # a list of strings
    if not shell:
        call_args = shlex.split(call_args) if isinstance(call_args, str) else call_args

    # Exec run information/logging
    logger.info("exec_cmd: %s", cmd)
    logger.debug("Call arguments: %s", call_args)
    debug_args = subprocess_args.copy()
    debug_args["env"] = "*hidden*"
    logger.debug("Subprocess args: %s", debug_args)

    # Run the process
    sys.stdout.flush()
    proc = subprocess.run(call_args, check=False, **subprocess_args)

    # Check if the process failed
    if not ignore_rc and proc.returncode not in allowed_rc:

        # If the subprocess was called with stdout PIPE, output it here
        if subprocess_args["stdout"] is not None:
            print(str(proc.stdout), flush=True)

        raise CommandException(f"Process exited with bad exit code: {proc.returncode}")

    return proc


def run_action():

    try:

        parser = argparse.ArgumentParser(
            prog="entrypoint", description="entrypoint", exit_on_error=False
        )

        parser.add_argument(
            "-v", "-d", action="store_true", dest="debug", help="Verbose/debug output"
        )

        parser.add_argument(
            "action",
            action="store",
            help="Action to perform"
        )

        parser.add_argument(
            "action_arg",
            action="store",
            help="Optional argument to the action",
            default="",
            nargs=argparse.REMAINDER
        )

        args = parser.parse_args()

        # Make sure we have an action
        if args.action is None or args.action == "":
            raise ArgumentException("Invalid or missing action")

        # Check that it is a known action
        if args.action not in actions:
            raise ArgumentException(f"Unknown action {args.action}")

        # Run the action, with the optional action argument
        actions[args.action](" ".join(args.action_arg))

        sys.exit(0)

    except Exception as e:
        sys.stdout.flush()
        print(e, flush=True)
        sys.exit(1)


# Build helpers
class ContainerSession:
    def __init__(self, container_image):
        self.engine = "docker"
        self.docker_file = "src/Dockerfile"
        self.container_dir = "src"
        self.container_image = container_image
        self.build_args = []
        self.tags = []
        self.other_build_args = ""

    def build(self):

        cmd = [f"{self.engine} build -f {self.docker_file} {self.container_dir}"]
        cmd = cmd + [f"--build-arg {x}" for x in self.build_args]
        cmd = cmd + [f"--tag {self.container_image}:{x}" for x in self.tags]
        cmd = cmd + [self.other_build_args]

        bash(" ".join(cmd))

    def login(self, registry, username, password):
        bash(f"{self.engine} login --password-stdin"
            f" -u \"{username}\""
            f" \"{registry}\" <"
            " <(echo \"$REGISTRY_PASSWORD\")", env={
                "REGISTRY_PASSWORD": password
            })

    def push(self):
        for tag in self.tags:
            bash(f"{self.engine} push {self.container_image}:{tag}")


class PythonSession:
    def __init__(self):
        self.venv = "venv"
        self.dist = "dist"
        self.requirements = "requirements.txt"

    def setup(self):

        # Configure venv and install base pip packages
        bash(f"python3 -m venv {self.venv} && "
            f"source {self.venv}/bin/activate && "
            "python3 -m pip install --upgrade pip && "
            "python3 -m pip install --upgrade setuptools build twine")

        if os.path.isfile(self.requirements):
            bash(f"source {self.venv}/bin/activate && "
                f"python3 -m pip install -r {self.requirements}")

    def build(self):
        bash(f"source {self.venv}/bin/activate && "
            "python3 -m build")

    def clean_venv(self):
        bash(f"rm -rf {self.venv}")

    def clean_dist(self):
        bash(f"rm -rf {self.dist}")

    def publish(self, username, password):
        bash("source ./venv/bin/activate && "
            f"python3 -m twine upload {self.dist}/*", env={
                "TWINE_USERNAME": username,
                "TWINE_PASSWORD": password
            })


class DeployEnvironment:
    def __init__(self, name):
        self.name = name
        self.next_env = ""


class DeploySession:
    def __init__(self):

        self._environments = {}

    def define_env(self, env_name, *, next_env=""):

        self._environments[env_name] = DeployEnvironment(env_name)
        self._environments[env_name].next_env = next_env

    def get_env(self, env_name):
        if env_name not in self._environments:
            raise DeployError(f"Environment does not exist: {env_name}")

        return self._environments[env_name]

    def promote_forgejo(self, env_name, *, server_url, repo, promote_token):

        val_arg(isinstance(env_name, str), "Invalid env name parameter. Must be str")
        val_arg(isinstance(server_url, str), "Invalid server url parameter. Must be str")
        val_arg(isinstance(repo, str), "Invalid repo parameter. Must be str")
        val_arg(isinstance(promote_token, str), "Invalid promote token parameter. Must be str")

        val_arg(env_name != "", "Empty env name supplied")
        val_arg(server_url != "", "Empty server url")
        val_arg(repo != "", "Empty repo parameter")
        val_arg(promote_token != "", "Empty promote token")

        # Fetch all for git repo. Best effort
        bash("git fetch --all", ignore_rc=True)

        # Get environment
        env = self.get_env(env_name)
        next_env = env.next_env

        # Stop here if we have no next environment
        if next_env == "":
            logger.info("No next environment to promote to")
            return

        # Forgejo headers
        headers = {
            "Authorization": f"token {promote_token}",
            "Content-Type": "application/json"
        }

        # Post body
        body = textwrap.dedent(f"""\
        {{
            "title": "Promote { env.name } to { next_env }",
            "base": "{ next_env }",
            "head": "{ env.name }",
            "description": "Promote { env.name } to { next_env }"
        }}
        """)

        args = {
            "url": f"{server_url}/api/v1/repos/{repo}/pulls",
            "method": "post",
            "body": body,
            "headers": headers,
            "verify": False,
            # Create with an existing PR generates a 409 response
            "status_check": [201, 409]
        }

        logger.debug("Url args: %s", args)
        Util.url_request(**args)


class BuildSession:
    def __init__(self, version_sources=None):

        # Default version and make sure version_sources is a list
        if version_sources is None:
            logger.info("Using default version: v0.1.0")
            version_sources = ["v0.1.0"]
        else:
            if not isinstance(version_sources, list):
                version_sources = [version_sources]

        self.update_version(version_sources)

    def update_version(self, sources, *, required=True, discard_regex=None, ignore_regex=None):

        # Check incoming parameters
        val_arg(isinstance(required, bool), "required parameter must be bool")
        val_arg(isinstance(sources, list), "Sources parameter must be a list of strings")
        val_arg(all(isinstance(x, str) for x in sources), "Some values in source are not strings")
        val_arg(isinstance(discard_regex, (list, type(None))), "discard_regex parameter must be list of string or None")

        # Discard regex defines what is to be stripped and is not considered
        # part of the version string
        if discard_regex is None:
            discard_regex = ["^refs/tags/"]

        # Ignore regex defines what is to be stripped before the semver check, but
        # is still considered part of the version string
        if ignore_regex is None:
            ignore_regex = ["^v"]

        # Regex for identifying and splitting semver strings
        # Reference: https://semver.org/
        semver_regex = r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\."
        semver_regex += r"(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>"
        semver_regex += r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
        semver_regex += r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
        semver_regex += r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+"
        semver_regex += r"(?:\.[0-9a-zA-Z-]+)*))?$"

        for source in sources:
            logger.info("Checking %s", source)

            # Strip any 'discard' components
            post_discard_source = source
            for regex_item in discard_regex:
                post_discard_source = re.sub(regex_item, "", post_discard_source)

            logger.debug("Source after discard strip: %s", post_discard_source)

            # Strip any 'ignore' components
            post_ignore_source = post_discard_source
            for regex_item in ignore_regex:
                post_ignore_source = re.sub(regex_item, "", post_ignore_source)

            logger.debug("Source after ignore strip: %s", post_ignore_source)

            # Check if this source is a semver match
            result = re.match(semver_regex, post_ignore_source)
            if result is None:
                logger.debug("Source (%s) is not a match", post_ignore_source)
                continue

            logger.info("Semver match on %s", post_ignore_source)

            # Assign semver components to environment vars
            self.v_source = source
            self.v_stripped = post_discard_source
            self.v_base = post_ignore_source
            self.v_full = "" if result[0] is None else result[0]
            self.v_major = "" if result[1] is None else result[1]
            self.v_minor = "" if result[2] is None else result[2]
            self.v_patch = "" if result[3] is None else result[3]
            self.v_prerelease = "" if result[4] is None else result[4]
            self.v_buildmeta = "" if result[5] is None else result[5]
            self.v_is_prerelease = result[4] is not None

            # Version string for mmp (major, minor, patch)
            self.v_mmp = self.v_major
            if self.v_mmp != "":
                if self.v_minor != "":
                    self.v_mmp = self.v_mmp + f".{self.v_minor}"

                    if self.v_patch != "":
                        self.v_mmp = self.v_mmp + f".{self.v_patch}"

            return

        # No matches found
        if required:
            raise VersionParseError("No semver matches found")


class Util:
    @classmethod
    def fail(cls, message):
        raise BuildFailure(message)

    @classmethod
    def git_sha(cls):
        return exec_cmd("git log -1 --format=%h", capture=True).stdout

    @classmethod
    def git_latest_tag(cls):
        return bash("git describe --tags --abbrev=0 2>/dev/null "
                    "|| echo \"\"", capture=True).stdout

    @classmethod
    def git_list_tags(cls):
        return exec_cmd("git tag --sort v:refname", capture=True)

    @classmethod
    def git_notes_since_tag(cls, tag=None):

        if tag is None or tag == "":
            return bash("git log --no-merges --format=%s HEAD", capture=True).stdout
        else:
            return bash(f"git log --no-merges --format=%s HEAD \"^{tag}\"", capture=True).stdout

    @classmethod
    def url_request(cls, url, method="get", headers=None, body=None, verify=True, status_check=None):

        val_arg(isinstance(url, str), "Invalid url. Must be string")
        val_arg(isinstance(method, str), "Invalid method. Must be string")
        val_arg(isinstance(body, (str, type(None))), "Invalid body. Must be None or str")
        val_arg(isinstance(verify, bool), "Invalid verify parameter. Must be bool")

        val_arg(isinstance(status_check, (list, int, type(None))), "Invalid status_check parameter. Must be list, int or None")

        # Wrap single int in a list
        if isinstance(status_check, int):
            status_check = [status_check]

        # Make sure all status checks are ints
        if status_check is not None:
            val_arg(all([isinstance(x, int) for x in status_check]), "Invalid values in status check. Must be int")

        if headers is None:
            headers = {}

        val_arg(isinstance(headers, dict), "Invalid headers. Must be dict type")

        for key in headers:
            val_arg(isinstance(key, str), "Invalid keys in headers. Must be string")
            val_arg(isinstance(headers[key], str), "Invalid values in headers. Must be string")

        # Perform request
        args = {
            "method": method,
            "url": url,
            "timeout": (10, 30),
            "headers": headers,
            "verify": verify,
        }

        if body is not None:
            args["data"] = body

        response = requests.request(**args)

        logger.debug("Response code: %s", response.status_code)
        logger.debug("Response text: %s", response.text)

        if status_check is None:
            # Use default handling for response codes
            response.raise_for_status()
        else:
            if response.status_code not in status_check:
                raise requests.HTTPError(
                    f"Status code ({response.status_code}) not in {status_check}"
                )

        return response

    @classmethod
    def github_release(cls, tag_name, *, name, prerelease, github_token, repo):

        val_arg(isinstance(tag_name, str), "Invalid tag name parameter. Must be str")
        val_arg(isinstance(name, str), "Invalid name parameter. Must be str")
        val_arg(isinstance(prerelease, bool), "Invalid prerelease parameter. Must be bool")
        val_arg(isinstance(github_token, str), "Invalid github token parameter. Must be str")
        val_arg(isinstance(repo, str), "Invalid repo parameter. Must be str")

        val_arg(tag_name != "", "Empty tag name supplied")
        val_arg(name != "", "Empty name supplied")
        val_arg(github_token != "", "Empty github token")
        val_arg(repo != "", "Empty repo parameter")

        # GitHub headers
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {github_token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Post body
        body = json.dumps({
            "tag_name": tag_name,
            "name": name,
            "draft": False,
            "prerelease": prerelease,
            "generate_release_notes": True
        })

        args = {
            "url": f"https://api.github.com/repos/{repo}/releases",
            "method": "post",
            "body": body,
            "headers": headers,
            "status_check": [201]
        }

        logger.debug("Url args: %s", args)
        Util.url_request(**args)


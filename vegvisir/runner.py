from datetime import datetime
import logging
import sys
import subprocess
from typing import List
import json
import tempfile
import re
import shutil

from .implementation import Implementation, Role
from .testcase import Status, TestCase, TestResult

class LogFileFormatter(logging.Formatter):
	def format(self, record):
		msg = super(LogFileFormatter, self).format(record)
		# remove color control characters
		return re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]").sub("", msg)

class Runner:
	_start_time: datetime = 0
	_end_time: datetime = 0

	_clients: List[Implementation] = []
	_servers: List[Implementation] = []
	_shapers: List[Implementation] = []

	_logger: logging.Logger = None
	_debug: bool = False
	_save_files: bool = False
	_log_dir: str = ""

	def __init__(
		self,
		implementations_file: str,
		debug: bool = False
	):
		self._logger = logging.getLogger()
		self._logger.setLevel(logging.DEBUG)
		console = logging.StreamHandler(stream=sys.stderr)
		if debug:
			console.setLevel(logging.DEBUG)
		else:
			console.setLevel(logging.INFO)
		self._logger.addHandler(console)

		self._read_implementations_file(implementations_file)

	def _read_implementations_file(self, file: str):
		self._clients = []
		self._servers = []
		self._shapers = []

		with open(file) as f:
			implementations = json.load(f)

		logging.debug("Loading implementations:")
		
		for name in implementations:
			attrs = implementations[name]

			roles = []
			to_add = []
			for role in attrs["role"]:
				if role == "client":
					roles.append(Role.CLIENT)
					to_add.append(self._clients)
				elif role == "server":
					roles.append(Role.SERVER)
					to_add.append(self._servers)
				elif role == "shaper":
					roles.append(Role.SHAPER)
					to_add.append(self._shapers)

			impl = Implementation(name, attrs["image"], attrs["url"], roles)

			for lst in to_add:
				lst.append(impl)

			logging.debug("\tloaded %s as %s", name, attrs["role"])

	def run(self) -> int:
		self._start_time = datetime.now()
		self._log_dir = "logs_{:%Y-%m-%dT%H:%M:%S}".format(self._start_time)
		nr_failed = 0

		for shaper in self._shapers:
			for server in self._servers:
				for client in self._clients:
					logging.debug("running with shaper %s (%s), server %s (%s), and client %s (%s)",
					shaper.name, shaper.image,
					server.name, server.image,
					client.name, client.image
					)

					testcase = TestCase()
					testcase.name = "test"

					result = self._run_test(shaper, server, client, testcase)
					logging.debug("\telapsed time since start of test: %s", str(result.end_time - result.start_time))

		self._end_time = datetime.now()
		logging.info("elapsed time since start of run: %s", str(self._end_time - self._start_time))
		return nr_failed

	def _run_test(
		self,
		shaper: Implementation,
		server: Implementation, 
		client: Implementation,
		testcase: TestCase
		) -> TestResult:
		result = TestResult()
		result.start_time = datetime.now()

		sim_log_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="logs_sim_")
		server_log_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="logs_server_")
		client_log_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="logs_client_")
		log_file = tempfile.NamedTemporaryFile(dir="/tmp", prefix="output_log_")
		log_handler = logging.FileHandler(log_file.name)
		log_handler.setLevel(logging.DEBUG)

		formatter = LogFileFormatter("%(asctime)s %(message)s")
		log_handler.setFormatter(formatter)
		logging.getLogger().addHandler(log_handler)

		params = (
			"WAITFORSERVER=server:443 "

			"CLIENT=" + client.image + " "
			"TESTCASE_CLIENT=transfer" + " "
			"CLIENT_PARAMS=\"--ca-certs tests/pycacert.pem -q /logs/qlog\"" + " "
			"REQUESTS=\"https://193.167.100.100:443/\"" + " "

			"DOWNLOADS=" + "./downloads" + " "
			"SERVER=" + server.image + " "
			"TESTCASE_SERVER=" + " "
			"SERVER_PARAMS=\"--certificate tests/ssl_cert.pem --private-key tests/ssl_key.pem -q /logs/qlog\"" + " "
			"WWW=" + "./www" + " "
			"CERTS=" + "./certs" + " "

			"SHAPER=" + shaper.image + " "
			"SCENARIO=\"simple-p2p --delay=15ms --bandwidth=10Mbps --queue=25\"" + " "

			"SERVER_LOGS=" + "/logs" + " "
			"CLIENT_LOGS=" + "/logs" + " "
		)
		containers = "sim client server"

		cmd = (
			params
			+ " docker-compose up --abort-on-container-exit --timeout 1 "
			+ containers
		)

		result.status = Status.FAILED
		try:
			logging.debug("running command: %s", cmd)
			proc = subprocess.run(
				cmd,
				shell=True,
				stdout=subprocess.PIPE,
				stderr=subprocess.STDOUT,
				timeout=30
			)
			logging.debug("proc: %s", proc.stdout.decode("utf-8"))
			result.status = Status.SUCCES
		except subprocess.TimeoutExpired as e:
			logging.debug("subprocess timeout: %s", e.stdout.decode("utf-8"))
		except Exception as e:
			logging.debug("subprocess error: %s", str(e))

		self._copy_logs("sim", sim_log_dir)
		self._copy_logs("client", client_log_dir)
		self._copy_logs("server", server_log_dir)

		# save logs
		logging.getLogger().removeHandler(log_handler)
		log_handler.close()
		if result.status == Status.FAILED or result.status == Status.SUCCES:
			log_dir = self._log_dir + "/" + server.name + "_" + client.name + "/" + testcase.name
			shutil.copytree(server_log_dir.name, log_dir + "/server")
			shutil.copytree(client_log_dir.name, log_dir + "/client")
			shutil.copytree(sim_log_dir.name, log_dir + "/sim")
			shutil.copyfile(log_file.name, log_dir + "/output.txt")
			if self._save_files and result.status == Status.FAILED:
				shutil.copytree(testcase.www_dir(), log_dir + "/www")
				try:
					shutil.copytree(testcase.download_dir(), log_dir + "/downloads")
				except Exception as exception:
					logging.info("Could not copy downloaded files: %s", exception)

		server_log_dir.cleanup()
		client_log_dir.cleanup()
		sim_log_dir.cleanup()

		result.end_time = datetime.now()
		return result


	def _copy_logs(self, container: str, dir: tempfile.TemporaryDirectory):
		r = subprocess.run(
			'docker cp "$(docker-compose --log-level ERROR ps -q '
			+ container
			+ ')":/logs/. '
			+ dir.name,
			shell=True,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
		)
		if r.returncode != 0:
			logging.info(
				"Copying logs from %s failed: %s", container, r.stdout.decode("utf-8")
			)
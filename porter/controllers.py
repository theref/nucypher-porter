import inspect
import json
from abc import ABC, abstractmethod
from json import JSONDecodeError
from pathlib import Path
from typing import Optional

import maya
from flask import Flask, Response
from hendrix.deploy.base import HendrixDeploy
from hendrix.deploy.tls import HendrixDeployTLS

from nucypher.config.constants import MAX_UPLOAD_CONTENT_LENGTH
from nucypher.network.resources import get_static_resources
from nucypher.utilities.concurrency import WorkerPoolException
from nucypher.utilities.emitters import StdoutEmitter
from nucypher.utilities.logging import Logger, GlobalLoggerSettings
from porter.emitters import WebEmitter


class ControllerBase(ABC):
    """
    A transactional interface for a human to interact with.
    """
    _emitter_class = NotImplemented

    def __init__(self, interface):
        # Control Emitter
        self.emitter = self._emitter_class()

        # Interface
        self.interface = interface

    def _perform_action(self, action: str, request: Optional[dict] = None) -> dict:
        """
        This method is where input validation and method invocation
        happens for all interface actions.
        """
        request = request or {}  # for requests with no input params request can be ''
        method = getattr(self.interface, action, None)
        serializer = method._schema
        params = serializer.load(request) # input validation will occur here.
        response = method(**params)  # < ---- INLET

        return serializer.dump(response)


class InterfaceControlServer(ControllerBase):
    _emitter_class = StdoutEmitter
    _crash_on_error_default = False

    def __init__(self,
                 app_name: str,
                 crash_on_error: bool = _crash_on_error_default,
                 *args,
                 **kwargs):
        super().__init__(*args, **kwargs)

        self.app_name = app_name

        # Configuration
        self.crash_on_error = crash_on_error

        def set_method(name):
            def wrapper(request=None, **kwargs):
                request = request or kwargs
                return self.handle_request(name, request)

            setattr(self, name, wrapper)

        for method_name in self._get_interfaces().keys():
            set_method(method_name)
            set_method(method_name)

        self.log = Logger(app_name)

    def _get_interfaces(self):
        return {
            name: method for name, method in
            inspect.getmembers(
                self.interface,
                predicate=inspect.ismethod)
            if hasattr(method, '_schema')
        }

    @abstractmethod
    def make_control_transport(self):
        return NotImplemented

    @abstractmethod
    def handle_request(self, method_name, control_request):
        return NotImplemented

    @abstractmethod
    def test_client(self):
        return NotImplemented


class CLIController(InterfaceControlServer):

    _emitter_class = StdoutEmitter

    def make_control_transport(self):
        return

    def test_client(self):
        return

    def handle_request(self, method_name, request) -> dict:
        response = self._perform_action(action=method_name, request=request)
        if GlobalLoggerSettings._json_ipc:
            # support for --json-ipc flag, for JSON *responses* from CLI commands-as-requests.
            start = maya.now()
            self.emitter.ipc(response=response, request_id=start.epoch, duration=maya.now() - start)
        else:
            self.emitter.pretty(response)
        return response


class WebController(InterfaceControlServer):
    """
    A wrapper around a JSON control interface that
    handles web requests to exert control over an implemented interface.
    """

    _emitter_class = WebEmitter
    _crash_on_error_default = False

    _captured_status_codes = {200: 'OK',
                              400: 'BAD REQUEST',
                              404: 'NOT FOUND',
                              500: 'INTERNAL SERVER ERROR'}

    def test_client(self):
        test_client = self._transport.test_client()

        # ease your mind
        self._transport.config.update(TESTING=self.crash_on_error, PROPOGATE_EXCEPTION=self.crash_on_error)

        return test_client

    def make_control_transport(self):
        self._transport = Flask(self.app_name)
        self._transport.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_CONTENT_LENGTH

        # Return FlaskApp decorator
        return self._transport

    def start(self,
              port: int,
              tls_key_filepath: Path = None,
              tls_certificate_filepath: Path = None,
              dry_run: bool = False):
        if dry_run:
            return

        if tls_key_filepath and tls_certificate_filepath:
            self.log.info("Starting HTTPS Control...")
            # HTTPS endpoint
            hx_deployer = HendrixDeployTLS(action="start",
                                           key=str(tls_key_filepath.absolute()),
                                           cert=str(tls_certificate_filepath.absolute()),
                                           options={
                                               "wsgi": self._transport,
                                               "https_port": port,
                                               "resources": get_static_resources()
                                           })
        else:
            # HTTP endpoint
            # TODO #845: Make non-blocking web control startup
            self.log.info("Starting HTTP Control...")
            hx_deployer = HendrixDeploy(action="start",
                                        options={
                                            "wsgi": self._transport,
                                            "http_port": port,
                                            "resources": get_static_resources()
                                        })

        hx_deployer.run()  # <--- Blocking Call to Reactor

    def __call__(self, *args, **kwargs):
        return self.handle_request(*args, **kwargs)

    @staticmethod
    def json_response_from_worker_pool_exception(exception):
        json_response = {
            'failure_message': str(exception)
        }
        if exception.failures:
            failures = [
                {'value': value, 'error': str(exc_info[1])}
                for value, exc_info in exception.failures.items()
            ]
            json_response['failures'] = failures

        return json_response

    def handle_request(self, method_name, control_request, *args, **kwargs) -> Response:
        _400_exceptions = (ValueError,
                           TypeError,
                           JSONDecodeError,
                           self.emitter.MethodNotFound)

        try:
            request_data = control_request.data
            request_body = json.loads(request_data) if request_data else {}

            # handle query string parameters
            if hasattr(control_request, 'args'):
                request_body.update(control_request.args)

            request_body.update(kwargs)

            if method_name not in self._get_interfaces():
                raise self.emitter.MethodNotFound(f'No method called {method_name}')

            response = self._perform_action(action=method_name, request=request_body)

        except _400_exceptions as e:
            __exception_code = 400
            return self.emitter.exception(
                e=e,
                log_level='debug',
                response_code=__exception_code,
                error_message=WebController._captured_status_codes[__exception_code])

        except WorkerPoolException as e:
            # special case since WorkerPoolException contains multiple stack traces
            # - not ideal for returning from REST endpoints
            __exception_code = 404
            if self.crash_on_error:
                raise

            json_response_from_exception = self.json_response_from_worker_pool_exception(e)
            return self.emitter.exception_with_response(
                json_error_response=json_response_from_exception,
                e=RuntimeError(json_response_from_exception['failure_message']),
                error_message=WebController._captured_status_codes[__exception_code],
                response_code=__exception_code,
                log_level='warn')

        except Exception as e:
            __exception_code = 500
            if self.crash_on_error:
                raise
            return self.emitter.exception(
                e=e,
                log_level='debug',
                response_code=__exception_code,
                error_message=WebController._captured_status_codes[__exception_code])

        else:
            self.log.debug(f"{method_name} [200 - OK]")
            return self.emitter.respond(json_response=response)


class PorterCLIController(CLIController):

    _emitter_class = StdoutEmitter

    def __init__(self,
                 interface: 'PorterInterface',
                 *args,
                 **kwargs):
        super().__init__(interface=interface, *args, **kwargs)

    def _perform_action(self, *args, **kwargs) -> dict:
        try:
            response_data = super()._perform_action(*args, **kwargs)
        finally:
            self.log.debug(f"Finished action '{kwargs['action']}', stopping {self.interface.implementer}")
            self.interface.implementer.disenchant()
        return response_data

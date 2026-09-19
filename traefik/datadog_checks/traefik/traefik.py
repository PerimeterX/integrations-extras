# (C) Datadog, Inc. 2018
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import re
import requests
from six import iteritems
from urllib.parse import urlparse, urlunparse

from datadog_checks.base import AgentCheck, ConfigurationError


def build_validated_url(base_url: str, path: str) -> str:
    try:
        # Minimal path validation
        if "/../" in base_url or re.search(r"/%2e%2e/", base_url, re.IGNORECASE):
            raise ValueError("Invalid path")
        if "/../" in path or re.search(r"/%2e%2e/", path, re.IGNORECASE):
            raise ValueError("Invalid path")
        
        parsed = urlparse(base_url)
        
        # Protocol + host checks
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Invalid protocol")
        if not parsed.hostname:
            raise ValueError("Invalid host")
        allowed_domains = ["example.com"]  # add your allowed domains here
        if parsed.hostname.lower() not in allowed_domains:
            raise ValueError("Invalid host")
        
        # Validate path parameter
        if not re.fullmatch(r"/[A-Za-z0-9_/-]*", path):
            raise ValueError("Invalid parameter")
        
        # Set the path
        parsed = parsed._replace(path=path)
        
        return urlunparse(parsed)
    except Exception:
        raise ValueError("Invalid URL")


class TraefikCheck(AgentCheck):
    def check(self, instance):
        host = instance.get('host')
        port = instance.get('port', '8080')
        path = instance.get('path', '/health')
        scheme = instance.get('scheme', 'http')

        if not host:
            self.warning('Configuration error, you must define `host`')
            raise ConfigurationError('Configuration error, you must define `host`')

        try:
            # Validate port
            port_int = int(port)
            if not 1 <= port_int <= 65535:
                raise ValueError("Invalid port")
            
            base_url = '{}://{}:{}'.format(scheme, host, port_int)
            url = build_validated_url(base_url, path)
            response = requests.get(url)
            response_status_code = response.status_code

            if response_status_code == 200:
                self.service_check('traefik.health', self.OK)

                payload = response.json()

                if 'total_status_code_count' in payload:
                    status_code_counts = payload['total_status_code_count']

                    for status_code, count in iteritems(status_code_counts):
                        self.gauge('traefik.total_status_code_count', count, ['status_code:{}'.format(status_code)])

                else:
                    self.log.warning('Field total_status_code_count not found in response.')

                if 'total_count' in payload:
                    self.gauge('traefik.total_count', payload['total_count'])
                else:
                    self.log.warning('Field total_count not found in response.')

            else:
                self.service_check(
                    'traefik.health', self.CRITICAL, message='Traefik health check return code is not 200'
                )

        except requests.exceptions.ConnectionError:
            self.service_check('traefik.health', self.CRITICAL, message='Traefik endpoint unreachable')

        except Exception as e:
            self.service_check('traefik.health', self.UNKNOWN, message='UNKNOWN exception: {}'.format(e))

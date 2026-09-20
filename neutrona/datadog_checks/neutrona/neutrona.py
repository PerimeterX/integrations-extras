import json
import re
from urllib.parse import urlparse, urlunparse, urlencode

import requests
from requests import RequestException

from datadog_checks.base import AgentCheck
from datadog_checks.base.errors import CheckException


def build_azure_management_url(base_url: str, subscription_id: str) -> str:
    try:
        # Minimal path validation
        if "/../" in base_url or re.search(r"/%2e%2e/", base_url, re.IGNORECASE):
            raise ValueError("Invalid path")
        
        parsed = urlparse(base_url)
        
        # Protocol + host checks
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Invalid protocol")
        if not parsed.hostname:
            raise ValueError("Invalid host")
        allowed_domains = ["management.azure.com", "login.microsoftonline.com"]
        if parsed.hostname.lower() not in allowed_domains:
            raise ValueError("Invalid host")
        
        # Validate path parameters
        if not re.fullmatch(r"[A-Za-z0-9_-]+", subscription_id):
            raise ValueError("Invalid parameter")
        
        # Rebuild path from fixed literals + validated segments
        parsed = parsed._replace(path=f"/subscriptions/{subscription_id}/providers/Microsoft.Network/expressRouteCircuits")
        
        # Add query parameters
        query = {"api-version": "2018-08-01"}
        parsed = parsed._replace(query=urlencode(query))
        
        return urlunparse(parsed)
    except Exception:
        raise ValueError("Invalid URL")


def build_neutrona_api_url(base_url: str, service_key: str) -> str:
    try:
        # Minimal path validation
        if "/../" in base_url or re.search(r"/%2e%2e/", base_url, re.IGNORECASE):
            raise ValueError("Invalid path")
        
        parsed = urlparse(base_url)
        
        # Protocol + host checks
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Invalid protocol")
        if not parsed.hostname:
            raise ValueError("Invalid host")
        allowed_domains = ["expressroutetelemetry.neutrona.com"]
        if parsed.hostname.lower() not in allowed_domains:
            raise ValueError("Invalid host")
        
        # Validate path parameters
        if not re.fullmatch(r"[A-Za-z0-9_-]+", service_key):
            raise ValueError("Invalid parameter")
        
        # Rebuild path from fixed literals + validated segments
        # Preserving the original ?= pattern in the path
        parsed = parsed._replace(path=f"/client/?={service_key}")
        
        return urlunparse(parsed)
    except Exception:
        raise ValueError("Invalid URL")


class NeutronaCheck(AgentCheck):
    def check(self, instance):

        neutrona_express_route_api_url = 'https://expressroutetelemetry.neutrona.com'
        azure_authentication_url = 'https://login.microsoftonline.com'
        azure_management_url = 'https://management.azure.com/'

        # TESTING
        try:
            neutrona_express_route_api_url = instance['azure']['testing']['neutrona_express_route_api_url']
            azure_authentication_url = instance['azure']['testing']['azure_authentication_url']
            azure_management_url = instance['azure']['testing']['azure_management_url']
        except KeyError:
            pass

        # AZURE API OAUTH2 TOKEN
        try:
            directory_id = instance['azure']['directory_id']
            application_id = instance['azure']['application_id']
            application_key = instance['azure']['application_key']
            subscription_id = instance['azure']['subscription_id']
        except KeyError:
            self.log.error("Configuration error, please fix check's conf.yaml")
            raise CheckException("Configuration error, please fix check's conf.yaml")

        try:
            response = requests.post(
                '/'.join([azure_authentication_url.strip('/'), directory_id, 'oauth2/token?api-version=1.0']),
                data={
                    'grant_type': 'client_credentials',
                    'resource': 'https://management.core.windows.net/',
                    'client_id': application_id,
                    'client_secret': application_key,
                },
            )
        except RequestException:
            self.log.error(
                ' '.join(['Connection error to', azure_authentication_url, 'Unable to obtain Azure access token.'])
            )
            raise CheckException(
                ' '.join(['Connection error to', azure_authentication_url, 'Unable to obtain Azure access token.'])
            )

        azure_access_token = ''

        if response.status_code != 200:

            self.log.error(' '.join(['Error authenticating with Azure: ', str(response.status_code)]))
            raise CheckException(
                ' '.join(['Connection error to', azure_authentication_url, 'Unable to obtain Azure access token.'])
            )

        else:

            try:
                azure_access_token = json.loads(response.content)['access_token']
            except KeyError:
                self.log.error('Unable to obtain Azure access token. Access token not present in response.')
                raise CheckException('Unable to obtain Azure access token. Access token not present in response.')

        # EXPRESS ROUTE CROSS CONNECTIONS
        try:
            response = requests.get(
                build_azure_management_url(azure_management_url, subscription_id),
                headers={'Authorization': ' '.join(['Bearer', azure_access_token])},
            )
        except RequestException:
            self.log.error(' '.join(['Connection error to', azure_management_url]))
            raise CheckException(' '.join(['Connection error to', azure_management_url]))

        if response.status_code != 200:

            self.log.error(''.join(['Error querying Azure API: Code ', response.status_code]))
            raise CheckException(' '.join(['Connection error to', azure_management_url]))

        else:

            inventory = json.loads(response.content)

            try:

                for cc in inventory['value']:

                    service_key = cc['properties']['serviceKey']

                    service_provider_name = cc['properties']['serviceProviderProperties']['serviceProviderName']

                    if service_provider_name == 'Neutrona Networks':
                        self.log.info(' '.join(['Querying for:', service_key, service_provider_name]))

                        # NEUTRONA TELEMETRY DATA
                        try:
                            response = requests.get(
                                build_neutrona_api_url(neutrona_express_route_api_url, service_key),
                                # headers={
                                #    'Authorization': ' '.join(['Bearer', '']),
                                # }
                            )
                        except RequestException:
                            self.log.error(' '.join(['Connection error to', neutrona_express_route_api_url]))
                            raise CheckException(' '.join(['Connection error to', neutrona_express_route_api_url]))

                        if response.status_code == 200:

                            connections = json.loads(response.content)

                            try:
                                for conn in connections:
                                    for metric, value in conn.items():
                                        if metric != 'tags':
                                            self.gauge(
                                                '.'.join(['neutrona', 'azure', 'expressroute', metric]),
                                                value,
                                                conn['tags'],
                                                service_key,
                                            )
                            except KeyError:
                                self.log.error(''.join(['Error querying Neutrona Express Route API: Invalid Response']))
                                raise CheckException('Error querying Neutrona Express Route API: Invalid Response')

                        else:
                            self.log.error(
                                ' -- '.join(
                                    [
                                        service_key,
                                        service_provider_name,
                                        'Error querying Neutrona API: Code ',
                                        str(response.status_code),
                                    ]
                                )
                            )
                            raise CheckException(' '.join(['Connection error to', neutrona_express_route_api_url]))

            except KeyError:
                self.log.error(''.join(['Error querying Azure API: Invalid Response']))
                raise CheckException('Error querying Azure API: Invalid Response')

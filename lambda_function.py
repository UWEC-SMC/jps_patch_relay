import backoff
import boto3
import json
import os
import requests
from botocore.exceptions import ClientError

tdx_ticket_creation_endpoint = '/api/136/tickets?EnableNotifyReviewer=false&NotifyRequestor=false&NotifyResponsible=false&AllowRequestorCreation=false&applyDefaults=true'
tdx_manager = None

exceptions_file = open('patch_exceptions.json', 'r')
patch_exceptions = json.loads(exceptions_file.read())['exceptions']
exceptions_file.close()


def lambda_handler(event, context):
    global tdx_manager

    if not tdx_manager:
        env = os.environ.get('ENVIRONMENT')
        constants_manager = ConstantsManager(env)
        tdx_manager = TdxManager(constants_manager)

    webhook_event = json.loads(event['body'])['event']

    pkg_name = webhook_event['name']
    pkg_version = webhook_event['latestVersion']
    pkg_mgm_link = webhook_event['reportUrls'][0]

    print(f'Received package update event {webhook_event}')

    for exception in patch_exceptions:
        if exception in pkg_name:
            print('Found exception for package. Exiting...')
            return {
                "statusCode": 202  # Accepted, but no action will be taken
            }

    json_body = {
        "Title": f"[MacOS] {pkg_name}: {pkg_version} is available",
        "FormID": 6403,
        "Description": f"JPS patch management page: {pkg_mgm_link}",
        "AccountID": 2917,
        "StatusID": 1097,
        "PriorityID": 406,
        "RequestorUid": "38caa1c7-bb95-ed11-ac20-0050f2f4deeb",
        "ResponsibleGroupID": 544,
        "ServiceID": 1193,
        "ServiceCategoryID": 568,
        "Attributes": [
            {
                "ID": 13614,
                "Name": "Package Name",
                "Value": pkg_name
            },
            {
                "ID": 13615,
                "Name": "Package Version",
                "Value": pkg_version
            }
        ]
    }

    print(f'TDx request JSON: {json.dumps(json_body)}')
    res = tdx_manager.make_custom_req(tdx_ticket_creation_endpoint, data=json.dumps(json_body))

    return {
        'statusCode': res.status_code
    }


class ConstantsManager:

    def __init__(self, env):
        self.env = env
        self.__client = boto3.client('ssm')

    def get_parameters(self, parameter_paths: [str]):
        return self.__client.get_parameters(
            Names=[f'/{self.env}/{x}' for x in parameter_paths],
            WithDecryption=True
        )['Parameters']

    def put_parameter(self, parameter_path: str, value, overwrite=False):
        self.__client.put_parameter(Name=f'/{self.env}/{parameter_path}', Value=value, Overwrite=overwrite)


class TdxManager:
    __values = {}
    __timeout = 3.5

    tdx_auth_endpoint = '/api/auth'
    tdx_headers = {'content-type': 'application/json', 'accept': 'application/json'}

    def __init__(self, constants_manager: ConstantsManager):
        self.__constants_manager = constants_manager

        for parameter in self.__constants_manager.get_parameters(['tdx_api_url', 'tdx_user', 'tdx_password',
                                                                  'tdx_token']):
            self.__values['/'.join(parameter['Name'].split('/')[2:])] = parameter

    @backoff.on_predicate(backoff.constant, lambda x: x.status_code == 429, jitter=None, interval=60)
    @backoff.on_exception(backoff.expo, (requests.exceptions.Timeout, requests.exceptions.ConnectionError), max_tries=3)
    def authenticate(self):
        res = requests.post(self.__values['tdx_api_url'][
                                'Value'] + self.tdx_auth_endpoint, headers=self.tdx_headers, data=json.dumps({'UserName':
                                                                                                                  self.__values[
                                                                                                                      'tdx_user'][
                                                                                                                      'Value'], 'Password':
                                                                                                                  self.__values[
                                                                                                                      'tdx_password'][
                                                                                                                      'Value']}), timeout=self.__timeout)

        if res.status_code == 200:
            self.__values['tdx_token']['Value'] = res.text
            self.__constants_manager.put_parameter('tdx_token', res.text, overwrite=True)
            self.tdx_headers['Authorization'] = f'Bearer {res.text}'

            print(f'tdx headers: {self.tdx_headers}')

        return res

    @backoff.on_predicate(backoff.constant, lambda x: x.status_code == 429, jitter=None, interval=60)
    @backoff.on_predicate(backoff.constant, lambda x: x.status_code == 401, jitter=None, interval=0.1)
    @backoff.on_exception(backoff.expo, (requests.exceptions.Timeout, requests.exceptions.ConnectionError), max_tries=3)
    def make_custom_req(self, endpoint: str, headers: {str: str} = None, data=None):
        if not self.__values['tdx_token']['Value']:
            self.authenticate()

        print(f"[make_custom_req] Api url value: {self.__values['tdx_api_url']['Value'] + endpoint}")

        res = requests.post(self.__values['tdx_api_url'][
                                'Value'] + endpoint, headers=headers if headers != None else self.tdx_headers, data=data, timeout=self.__timeout)

        print(f'[make_custom_req] Custom req response: {res.text}')

        if res.status_code == 401:
            # A 401 means out token is no longer valid, so clear the currently stored one.
            self.__values['tdx_token']['Value'] = ''

        return res

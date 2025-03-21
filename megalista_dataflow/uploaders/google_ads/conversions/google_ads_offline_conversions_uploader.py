# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from apache_beam.options.value_provider import ValueProvider

from error.error_handling import ErrorHandler
from models.execution import Batch, Execution, AccountConfig, Destination
from models.oauth_credentials import OAuthCredentials
from uploaders import utils
from utils.utils import Utils
from uploaders.google_ads import ADS_API_VERSION
from uploaders.uploaders import MegalistaUploader

_DEFAULT_LOGGER: str = 'megalista.GoogleAdsOfflineConversionsUploader'


class GoogleAdsOfflineUploaderDoFn(MegalistaUploader):

  def __init__(self, oauth_credentials : OAuthCredentials, developer_token: ValueProvider, error_handler: ErrorHandler):
    super().__init__(error_handler)
    self.oauth_credentials = oauth_credentials
    self.developer_token = developer_token

  def _get_ads_service(self, customer_id: str):
    return utils.get_ads_service('GoogleAdsService', ADS_API_VERSION,
                                     self.oauth_credentials,
                                     self.developer_token.get(),
                                     customer_id)

  def _get_oc_service(self, customer_id):
    return utils.get_ads_service('ConversionUploadService', ADS_API_VERSION,
                                     self.oauth_credentials,
                                     self.developer_token.get(),
                                     customer_id)

  def start_bundle(self):
    pass

  def _get_customer_id(self, account_config:AccountConfig, destination:Destination) -> str:
    """
      If the customer_id is present on the destination, returns it, otherwise defaults to the account_config info.
    """
    if len(destination.destination_metadata) >= 2 and len(destination.destination_metadata[1]) > 0:
      return Utils.filter_text_only_numbers(destination.destination_metadata[1])
    return account_config.google_ads_account_id

  def _get_login_customer_id(self, account_config: AccountConfig, destination: Destination) -> str:
    """
      If the customer_id in account_config is a mcc, then login with the mcc account id, otherwise use the customer id.
    """
    if account_config._mcc:
        return account_config.google_ads_account_id
    
    return self._get_customer_id(account_config, destination)

  @staticmethod
  def _assert_conversion_name_is_present(execution: Execution):
    destination = execution.destination.destination_metadata
    if len(destination) == 0:
      raise ValueError('Missing destination information. Found {}'.format(
          len(destination)))

    if not destination[0]:
      raise ValueError('Missing destination information. Received {}'.format(
          str(destination)))

  @utils.safe_process(
      logger=logging.getLogger('megalista.GoogleAdsOfflineUploader'))
  def process(self, batch: Batch, **kwargs):
    execution = batch.execution
    self._assert_conversion_name_is_present(execution)

    customer_id = self._get_customer_id(execution.account_config, execution.destination)
    
    # Retrieves the login-customer-id if mcc enabled
    login_customer_id = self._get_login_customer_id(execution.account_config, execution.destination)
    # Initiates OCI service
    oc_service = self._get_oc_service(login_customer_id)
    # Initiates ADS service
    ads_service = self._get_ads_service(login_customer_id)
    
    resource_name = self._get_resource_name(ads_service, customer_id, execution.destination.destination_metadata[0])

    response = self._do_upload(oc_service,
                    execution,
                    resource_name,
                    customer_id,
                    batch.elements)

    batch_with_successful_gclids = self._get_new_batch_with_successfully_uploaded_gclids(batch, response)
    if len(batch_with_successful_gclids.elements) > 0:
      return [batch_with_successful_gclids]

  def _do_upload(self, oc_service, execution, conversion_resource_name, customer_id, rows):
    logging.getLogger(_DEFAULT_LOGGER).info(f'Uploading {len(rows)} offline conversions on {conversion_resource_name} to Google Ads.')
    conversions = []
    for row in rows:
      conversion= {
        'conversion_action': conversion_resource_name,
        'conversion_date_time': utils.format_date(row['time']),
        'conversion_value': float(str(row['amount'])),
        'gclid': row['gclid'],
        'conversion_environment': row['conversion_environment']
      }
      #adds external attribution data if provided
      if 'external_attribution_credit' in row and 'external_attribution_model' in row:
        conversion['external_attribution_data'] = {
          'external_attribution_credit': float(str(row['external_attribution_credit'])),
          'external_attribution_model': row['external_attribution_model']
        }
      #adds consent data if provided
      if 'consent_ad_user_data' in row and 'consent_ad_personalization' in row:
        conversion['consent'] = {
          'ad_user_data': row['consent_ad_user_data'],
          'ad_personalization': row['consent_ad_personalization']
        }
      conversions.append(conversion)

    upload_data = {
      'customer_id': customer_id,
      'partial_failure': True,
      'validate_only': False,
      'conversions': conversions
    }

    response = oc_service.upload_click_conversions(request=upload_data)

    error_message = utils.print_partial_error_messages(_DEFAULT_LOGGER, 'uploading offline conversions', response)
    if error_message:
      self._add_error(execution, error_message)

    return response

  def _get_resource_name(self, ads_service, customer_id: str, name: str):
      query = f"SELECT conversion_action.resource_name FROM conversion_action WHERE conversion_action.name = '{name}'"
      response_query = ads_service.search_stream(customer_id=customer_id, query=query)
      for batch in response_query:
        for row in batch.results:
          return row.conversion_action.resource_name
      raise Exception(f'Conversion "{name}" could not be found on account {customer_id}')

  @staticmethod
  def _get_new_batch_with_successfully_uploaded_gclids(batch: Batch, response):
    def gclid_lambda(result): return result.gclid

    successful_gclids = list(map(gclid_lambda, filter(gclid_lambda, response.results)))
    successful_elements = list(filter(lambda element: element['gclid'] in successful_gclids, batch.elements))

    return Batch(batch.execution, successful_elements)

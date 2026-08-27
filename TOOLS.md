# Wavix MCP Tools — full catalogue

118 tools, generated from the [Wavix OpenAPI spec](https://github.com/Wavix/wavix-openapi) via [`FastMCP.from_openapi()`](https://github.com/jlowin/fastmcp). Arguments mirror request path / query / body fields.

If a tool listed here is missing from your client, refresh the connection: the live catalogue tracks the OpenAPI spec as it evolves.

For the high-level grouping and an overview table, see the [README](README.md#tools).

## SMS and MMS

- `sms_and_mms_sender_ids_list` — List Sender IDs
- `sms_and_mms_sender_ids_create` — Create a Sender ID
- `sms_and_mms_sender_ids_get` — Retrieve a Sender ID
- `sms_and_mms_sender_ids_delete` — Delete a Sender ID
- `sms_and_mms_opt_outs_list` — List opted-out phone numbers
- `sms_and_mms_opt_outs_create` — Create an opt-out
- `sms_and_mms_messages_list` — List messages
- `sms_and_mms_messages_send` — Send a message
- `sms_and_mms_messages_get` — Retrieve a message

## Call control

- `call_control_list` — List active calls
- `call_control_create` — Start a call
- `call_control_get` — Retrieve a call
- `call_control_update` — Update a call
- `call_control_delete` — End a call
- `call_control_answer` — Answer a call
- `call_control_audio_play` — Play audio
- `call_control_audio_stop` — Stop audio playback
- `call_control_collect` — Collect DTMF input

## Call recording

- `call_recording_list` — List call recordings
- `call_recording_get_by_call` — Retrieve a recording by call ID (returns a pre-signed download URL)
- `call_recording_get` — Retrieve a recording
- `call_recording_delete` — Delete a call recording

## Call streaming

- `call_control_streams_create` — Start call streaming
- `call_control_streams_delete` — Stop call streaming

## Call webhooks

- `call_webhooks_list` — List call webhooks
- `call_webhooks_create` — Create a call webhook
- `call_webhooks_delete` — Delete a call webhook

## CDRs

- `cdrs_list` — List CDRs
- `cdrs_search` — Search transcriptions
- `cdrs_retranscribe` — Transcribe a call recording
- `cdrs_transcription_get` — Retrieve a transcription
- `cdrs_transcriptions` — Retrieve a transcription
- `cdrs_get` — Retrieve a CDR

## Speech Analytics

- `speech_analytics_get` — Retrieve a transcription
- `speech_analytics_retranscribe` — Retranscribe a file
- `speech_analytics_file_get` — Retrieve the original file (returns a pre-signed download URL)

## 2FA

- `two_fa_verification_create` — Create a 2FA Verification
- `two_fa_sessions_list` — List 2FA verifications
- `two_fa_verification_resend` — Resend a code
- `two_fa_verification_check` — Validate a code
- `two_fa_verification_cancel` — Cancel a 2FA verification
- `two_fa_events_list` — List 2FA verification events

## My numbers

- `my_numbers_list` — List phone numbers
- `numbers_bulk_update` — Bulk update phone numbers
- `my_numbers_delete` — Release phone numbers
- `my_numbers_get` — Retrieve a phone number
- `numbers_update` — Update a phone number

## Buy

- `buy_countries_list` — List countries
- `buy_regions_list` — List regions
- `buy_cities_list` — List cities
- `buy_region_cities_list` — List region cities
- `buy_numbers_list` — List available phone numbers

## Cart

- `cart_get` — Retrieve the cart
- `cart_add` — Add to cart
- `cart_remove` — Remove from cart
- `cart_checkout` — Check out the cart

## Number validator

- `number_validator_get` — Validate a number
- `number_validator_create_bulk` — Validate multiple numbers
- `number_validator_results_get` — Retrieve validation results

## SIP trunks

- `sip_trunks_list` — List SIP trunks
- `sip_trunks_create` — Create a SIP trunk
- `sip_trunks_get` — Retrieve a SIP trunk
- `sip_trunks_update` — Update a SIP trunk
- `sip_trunks_delete` — Delete a SIP trunk

## 10DLC

### Brands

- `ten_dlc_brands_list` — List 10DLC Brands
- `ten_dlc_brands_create` — Register a 10DLC Brand
- `ten_dlc_brands_get` — Retrieve a 10DLC Brand
- `ten_dlc_brands_update` — Update a 10DLC Brand
- `ten_dlc_brands_delete` — Delete a 10DLC Brand
- `ten_dlc_brand_appeals_list` — List Brand identity appeals
- `ten_dlc_brand_appeals_create` — Appeal a 10DLC Brand identity verification
- `ten_dlc_brand_evidence_list` — List Brand evidence
- `ten_dlc_brand_evidence_upload` — Upload Brand evidence
- `ten_dlc_brand_evidence_get` — Download Brand evidence (returns a pre-signed download URL)
- `ten_dlc_brand_evidence_delete` — Delete Brand evidence
- `ten_dlc_brand_vettings_list` — List external vettings
- `ten_dlc_brand_vettings_create` — Request external vetting
- `ten_dlc_brand_vettings_import` — Import external vetting
- `ten_dlc_brand_vetting_appeals_list` — List external vetting appeals
- `ten_dlc_brand_vetting_appeals_create` — Appeal external vetting
- `ten_dlc_brand_usecase_qualify` — Qualify a 10DLC Brand for a use case

### Campaigns

- `ten_dlc_campaigns_list` — List 10DLC Campaigns
- `ten_dlc_brand_campaigns_list` — List Brand Campaigns
- `ten_dlc_brand_campaigns_create` — Register a 10DLC Campaign
- `ten_dlc_brand_campaigns_get` — Retrieve a 10DLC Campaign
- `ten_dlc_brand_campaigns_update` — Update a 10DLC Campaign
- `ten_dlc_brand_campaigns_delete` — Delete a 10DLC Campaign
- `ten_dlc_campaign_numbers_link` — Link a number to a 10DLC Campaign
- `ten_dlc_campaign_numbers_unlink` — Unlink phone number
- `ten_dlc_campaign_numbers_list` — List Campaign phone numbers
- `ten_dlc_campaigns_nudge` — Nudge a 10DLC Campaign review

### Event subscriptions

- `ten_dlc_subscriptions_list` — List event subscriptions
- `ten_dlc_subscriptions_create` — Subscribe to 10DLC events
- `ten_dlc_subscriptions_delete` — Delete event subscription

## Profile

- `profile_get` — Retrieve a profile
- `profile_update` — Update a profile
- `profile_config_get` — Retrieve account settings

## API Keys

- `api_keys_list` — List API keys
- `api_keys_create` — Create an API key
- `api_keys_update` — Update an API key
- `api_keys_delete` — Delete an API key

## Sub-accounts

- `sub_accounts_list` — List sub-accounts
- `sub_accounts_create` — Create a sub-account
- `sub_accounts_get` — Retrieve a sub-account
- `sub_accounts_update` — Update a sub-account
- `sub_accounts_transactions_list` — List sub-account transactions

## Billing

- `billing_transactions_list` — List transactions
- `billing_invoices_list` — List financial statements
- `billing_invoices_download` — Download statement PDF (returns a pre-signed download URL)

## Voice campaigns

- `voice_campaigns_create` — Trigger a scenario
- `voice_campaigns_get` — Retrieve a voice campaign

## Wavix Embeddable (WebRTC)

- `webrtc_tokens_list` — List widget tokens
- `webrtc_tokens_create` — Create a widget token
- `webrtc_tokens_get` — Retrieve a widget token
- `webrtc_tokens_update` — Update a widget token
- `webrtc_tokens_delete` — Delete a widget token

## Link shortener

- `link_shortener_create` — Create a short link
- `link_shortener_metrics_list` — List short link metrics


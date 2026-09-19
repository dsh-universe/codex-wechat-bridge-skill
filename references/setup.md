# Setup reference

## Dependencies

- Python 3.10+
- `requests`
- `cryptography` (only needed for AES-encrypted media send/receive; optional for text-only)

## iLink protocol (from Tencent's iLink Bot API)

- Base URL: `https://ilinkai.weixin.qq.com`
- Headers (authenticated calls):
  - `Content-Type: application/json`
  - `AuthorizationType: ilink_bot_token`
  - `Authorization: Bearer <token>`
  - `iLink-App-Id: bot`
  - `iLink-App-ClientVersion: <int>`
  - `X-WECHAT-UIN: <base64 random>`
- Every POST body includes `base_info: {"channel_version": "2.2.0"}`.

### QR login

1. `GET ilink/bot/get_bot_qrcode?bot_type=3` → `{qrcode, qrcode_img_content}`.
2. User scans `qrcode_img_content` with WeChat and confirms.
3. Poll `GET ilink/bot/get_qrcode_status?qrcode=<value>` until `status == "confirmed"`.
4. On confirmed, read `ilink_bot_id`, `bot_token`, `baseurl`, `ilink_user_id` and save them.

### Receive

`POST ilink/bot/getupdates` with `{get_updates_buf: <buf>}` long-polls for messages. The response
returns `msgs` and a new `get_updates_buf`. Each message carries `item_list` (text at `type == 1` →
`text_item.text`) and a `context_token` for replying.

### Send

`POST ilink/bot/sendmessage` with `{msg: {from_user_id, to_user_id, client_id, message_type: 2,
message_state: 2, item_list: [{type: 1, text_item: {text}}], context_token?}}`. The outbound reply
should echo the peer's latest `context_token`.

## Known limitations

- iLink bot identities are personal-WeChat bots, not ordinary contacts you can invite into groups.
- Most bot accounts do not receive ordinary group events; only DMs are reliable.
- WeChat requires requests to come from an in-region network; do not route WeChat through an overseas
  proxy.
- A fresh `context_token` is required to send to a user who has not messaged the bot recently;
  tokenless sends may be rejected. Have the user message the bot first to establish context.

## Security

- Never commit `config.json` or `state/`.
- Redact the bot `account_id`, `token`, and any private provider addresses before sharing.

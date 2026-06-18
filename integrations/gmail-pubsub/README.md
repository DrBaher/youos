# Real-time Gmail triage (Gmail watch → Pub/Sub → YouOS)

By default YouOS polls Gmail every ~15 min. With this, Gmail **pushes** a
notification the moment your mailbox changes and YouOS triages/drafts within
seconds — so the draft is waiting before you even open the thread.

```
Gmail mailbox ──watch──► Cloud Pub/Sub topic ──push──► https://<you>.ts.net/api/gmail/push?token=…
   (your GCP project)                                        (YouOS webhook, b282)
                                                                     │
                                                              debounced triage sweep
```

Gmail's notification carries only `{emailAddress, historyId}` (no message
content). YouOS uses it purely as a **trigger** for its normal fetch-unread →
draft sweep. The webhook (`app/api/gmail_push_routes.py`) is the YouOS side; the
topic/subscription/watch live in **your** Google Cloud project — set up below.

## Prerequisites
- YouOS exposed publicly via **Tailscale Funnel** (Pub/Sub runs on Google's
  servers and can't reach a tailnet-private instance). Funnel's `/` proxy already
  forwards to YouOS on `:8765`, so `…/api/gmail/push` is reachable — no extra
  serve route. (`tailscale funnel --bg 8765`; the same exposure the Gmail add-on
  needs.) Your `tailscale.hostname` must be configured so the Host check accepts
  the request.
- `gcloud` authenticated to a GCP project, and the Gmail API enabled there.

## 1. Enable the YouOS side (inert until both are set)
```sh
youos config set agent.gmail_push.token "$(openssl rand -hex 24)"   # a long random secret
youos config set agent.gmail_push.enabled true
youos config get agent.gmail_push.token                              # copy it for step 3
```

## 2. Create the Pub/Sub topic and let Gmail publish to it
```sh
gcloud pubsub topics create youos-gmail
# Gmail's system service account must be able to publish:
gcloud pubsub topics add-iam-policy-binding youos-gmail \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role=roles/pubsub.publisher
```

## 3. Create a PUSH subscription pointing at the YouOS webhook
The `?token=` must equal `agent.gmail_push.token` from step 1.
```sh
gcloud pubsub subscriptions create youos-gmail-sub \
  --topic=youos-gmail \
  --push-endpoint="https://<you>.<tailnet>.ts.net/api/gmail/push?token=<TOKEN>" \
  --ack-deadline=30
```

## 4. Register the Gmail watch (per account)
This tells Gmail to publish to your topic. It **expires after 7 days** — re-run it
on a schedule (cron / a `youos` wrapper). Easiest one-off is the
[OAuth Playground](https://developers.google.com/oauthplayground) (scope
`gmail.readonly` or `gmail.metadata`) or `gcloud`'s access token:
```sh
ACCESS_TOKEN=...   # an OAuth token for the mailbox, gmail.metadata scope
curl -s -X POST "https://gmail.googleapis.com/gmail/v1/users/me/watch" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"topicName":"projects/<PROJECT>/topics/youos-gmail","labelIds":["INBOX"]}'
# → {"historyId":"…","expiration":"…(ms epoch, ~7 days)"}
```

> Renewal automation (re-`watch` daily) and a `youos gmail-watch` CLI are a
> planned follow-up — for now, re-run step 4 weekly, or wrap it in cron.

## 5. Verify
- Unauthorised hits are rejected (good): a `POST /api/gmail/push` with a wrong/no
  `?token=` → `403`/`404`.
- Send yourself a test email → within seconds the nightly/agent log shows a
  `gmail_push` sweep, and a draft appears in the queue / Gmail Drafts.
- Bursts coalesce: many notifications in a minute → one sweep (the existing
  `agent.triage_min_interval_seconds` debounce).

## Turn it off
```sh
youos config set agent.gmail_push.enabled false
gcloud pubsub subscriptions delete youos-gmail-sub   # stop the pushes
# the Gmail watch lapses on its own after 7 days if you stop renewing it
```

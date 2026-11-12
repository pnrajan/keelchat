# keelchat plugin for Claude Code

The plugin connects Claude Code to your keelchat server's `/mcp` endpoint. Claude can then build, check, try and
launch agents for the tenant your key belongs to. It can't reach any other tenant.

## Install

Set the server address and a tenant key with the build scope, then start Claude Code:

```bash
export KEELCHAT_URL=http://localhost:8000 KEELCHAT_KEY=<tenant key>
claude
```

```text
/plugin marketplace add pnrajan/keelchat
/plugin install keelchat@keelchat
```

- `KEELCHAT_URL`: the server's base address. The plugin calls `$KEELCHAT_URL/mcp`.
- `KEELCHAT_KEY`: a tenant key, sent as `Authorization: Bearer <key>`.

## Skills

- **build-agent**: asks what the bot must do, drafts the policy, checks it, tries a few conversations and saves it once
  you agree.
- **fix-findings** (`/keelchat:fix-findings`): works through the fix tasks the judge found, filing issues and making
  fixes only with your yes.

More: [Use with Claude](../../docs/FEATURES.md#use-with-claude).

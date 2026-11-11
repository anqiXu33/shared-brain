# Round-trip checklist for the assistants

`tests/roundtrip_live.py` proves that the server, Supabase and OpenAI do the right thing. It cannot prove that an assistant actually *uses* them: that Claude web looks things up in a new conversation, that ChatGPT supersedes instead of saving a second copy, that Claude Code retires instead of deleting. This checklist does, by hand, in each client. It takes about ten minutes per client.

Run it after changing tool descriptions or the standing instructions in a client, and after reconnecting a connector.

Everything happens in the `roundtrip-check` project, which exists only for testing (the script registers it on its first run). Pick a new code word each time, e.g. a fruit and a number: `芒果-42`, then `菠萝-17`.

## Steps (in each client)

| # | Where | Type | Pass if |
|---|---|---|---|
| 1 | new conversation | 在 roundtrip-check 项目里记一下：现在的测试暗号是 芒果-42。 | `save_memory` with project `roundtrip-check`, kind `state`, and this client as `source` |
| 2 | **another** new conversation | roundtrip-check 现在的测试暗号是什么？ | calls `project_brief` or `search_memory` **before** answering, and answers 芒果-42 |
| 3 | same conversation | 暗号改成 菠萝-17 了。 | `save_memory` with `supersedes` = the id from step 1 (not a second unrelated entry, not `update_memory`) |
| 4 | new conversation | roundtrip-check 的测试暗号是什么？ | answers 菠萝-17 and does not mention 芒果-42 as current |
| 5 | same conversation | 这个测试暗号不用了，作废它。 | `retire_memory` on the 菠萝-17 entry (not `delete_memory`) |
| 6 | new conversation | roundtrip-check 现在有测试暗号吗？ | looks it up and says there is none at the moment |
| 7 | across clients | save a new code word in this client, ask for it in another one | the other client finds it |

Check the tool calls, not only the answer: an assistant can answer step 2 correctly from the conversation it is in, which is why steps 2, 4 and 6 must start a new conversation. In Claude web and Claude Code the tool calls are shown in the conversation; in ChatGPT they appear only in Work mode conversations (the connector does not load in normal chats). The viewer (`/viewer`, project `roundtrip-check`, with history on) shows what was actually saved, superseded and retired.

## Clean up

Run `python tests/roundtrip_live.py` (or Actions > Live round trip > Run workflow): it deletes everything left in `roundtrip-check` before its own check.

## Log

| Date | Client | Failed steps | Notes |
|---|---|---|---|
| | Claude web | | |
| | ChatGPT (Work mode) | | |
| | Claude Code | | |

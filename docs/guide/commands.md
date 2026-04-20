# Telegram Commands

OpenTask registers 25 commands. You can also just send any text message to queue it as a task.

## Basic

| Command | Action |
|---------|--------|
| *any text* | Queue as a new task (uses current project, agent, model) |
| `/status` | Show running task with elapsed time, model, and worker |
| `/queue` | List pending tasks with priorities |
| `/history [N]` | Recent completed/failed tasks (default 10) |
| `/cancel [id]` | Cancel the running task, or a specific pending task by ID |
| `/output <id>` | Get full output of a task (sent as file if large) |
| `/retry <id>` | Re-queue a failed task |
| `/help` | Show all commands |

## Configuration

| Command | Action |
|---------|--------|
| `/project <path>` | Set working directory (validated against allowlist) |
| `/agent <name>` | Switch agent (`opencode`, `claude`, etc.) |
| `/model <name>` | Set model for future tasks (`sonnet`, `opus`, `haiku`, etc.) |
| `/setmodel <id> <name>` | Change model for a specific pending task |

## Follow-Up Conversations

| Command | Action |
|---------|--------|
| `/continue <id> <prompt>` | Send a follow-up message to a completed task |
| `/cancel_followup` | Exit follow-up conversation mode |
| *Tap "Follow Up" button* | Enter follow-up mode for a completed task |

See [Follow-Up Conversations](guide/followups.md) for details.

## Task Chains

| Command | Action |
|---------|--------|
| `/savechain <name> step1 \| step2 \| step3` | Save a multi-step chain |
| `/chain <name>` | Run a saved chain |
| `/chains` | List all saved chains |
| `/delchain <name>` | Delete a chain |

See [Task Chains & Repeats](guide/chains.md) for details.

## Repeat Tasks

| Command | Action |
|---------|--------|
| `/repeat <N> <prompt>` | Run a task N times |
| `/repeat until:HH:MM <prompt>` | Run repeatedly until the specified time |

## Queue Management

| Command | Action |
|---------|--------|
| `/bump <id>` | Move a pending task to the front of the queue |
| `/search <query>` | Search tasks by prompt text |

## Recipes

| Command | Action |
|---------|--------|
| `/addrecipe <name> triggers:kw1,kw2 [agent:name] [model:name]` | Create a recipe |
| `/recipes` | List all recipes |
| `/recipe <name>` | Show recipe details |
| `/delrecipe <name>` | Delete a recipe |

See [Recipes](guide/recipes.md) for details.

## Persistent Reply Keyboard

After `/start`, a persistent keyboard appears at the bottom of the chat with quick-access buttons:

- `/status` — check running task
- `/queue` — view pending tasks
- `/history` — recent tasks
- `/cancel` — cancel current task
- `/help` — command reference

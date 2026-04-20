# Recipes

Recipes enable **smart prompt routing**. When you send a task, OpenTask checks if any recipe's trigger keywords match your prompt and automatically applies that recipe's configuration.

## What a Recipe Can Do

| Feature | Description |
|---------|-------------|
| **Trigger keywords** | Words that activate the recipe (e.g., `ros2`, `rosbag`, `analysis`) |
| **Agent override** | Use a specific agent for matched tasks |
| **Model override** | Use a specific model |
| **Project directory** | Run in a specific directory |
| **Setup commands** | Shell commands to prepend (e.g., `source /opt/ros/humble/setup.bash`) |
| **Skills** | Skill package names whose `SKILL.md` to inject into the prompt |
| **Prompt prefix/suffix** | Text to prepend or append to the user's prompt |

## Creating a Recipe

```
/addrecipe ros2 triggers:rosbag,ros2,gazebo agent:claude model:opus
```

Now sending "analyze the rosbag file" automatically uses Claude with the Opus model because "rosbag" matches a trigger.

### Full Syntax

```
/addrecipe <name> triggers:kw1,kw2 [agent:name] [model:name] [project:/path] [setup:cmd1;cmd2] [skills:skill1,skill2] [prefix:text] [suffix:text]
```

## How Matching Works

1. When you send a text message, OpenTask scans all recipes
2. Each recipe's trigger keywords are checked (case-insensitive) against your prompt
3. The recipe with the most keyword matches wins
4. You get inline buttons to **Use** or **Skip** the matched recipe

## Skills

Skills are loaded from `~/.taskpilot/skills/<skill-name>/SKILL.md` and injected into the prompt at execution time. Configure the skills directory with `SKILLS_DIR`.

## Managing Recipes

```
/recipes              — list all recipes
/recipe <name>        — show recipe details (triggers, agent, model, etc.)
/delrecipe <name>     — delete a recipe
```

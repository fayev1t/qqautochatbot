import nonebot
import logging

logging.basicConfig(level=logging.INFO)

nonebot.init()

# Load all plugins explicitly
nonebot.load_plugins("qqbot.plugins")

# Print loaded plugins
import nonebot.plugin
print(f"[startup] Loaded plugins: {nonebot.plugin.get_loaded_plugins()}")


if __name__ == "__main__":
    nonebot.run()



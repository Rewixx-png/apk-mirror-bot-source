module.exports = {
  apps: [
    {
      name: "apk-mirror-bot",
      script: "main.py",
      interpreter: "python3.12",
      cwd: "/root/apk_mirror_system",
      env_file: "/root/apk_mirror_system/.env",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      env: {
        PYTHONUNBUFFERED: "1",
        BOT_API_BASE: "http://localhost:18081",
        BOT_API_LOCAL: "1",
        BOT_API_DOCKER_COPY: "1",
        BOT_API_CONTAINER: "telegram_bot_api",
        GITHUB_OWNER: "Rewixx-png",
        STORAGE_REPO: "telegram-apk-storage",
        STORAGE_RELEASE_TAG: "apk-storage"
      }
    }
  ]
}

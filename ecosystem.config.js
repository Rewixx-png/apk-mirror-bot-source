module.exports = {
  apps: [
    {
      name: "apk-mirror-bot",
      script: "main.py",
      interpreter: "python3.11",
      cwd: "/root/apk_mirror_system",
      env_file: "/root/apk_mirror_system/.env",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      env: {
        PYTHONUNBUFFERED: "1",
        GITHUB_OWNER: "Rewixx-png",
        STORAGE_REPO: "telegram-apk-storage",
        STORAGE_RELEASE_TAG: "apk-storage"
      }
    }
  ]
}

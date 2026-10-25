module.exports = {
  apps: [
    {
      name: 'worldwarbot',
      script: 'main.py',
      interpreter: 'python3',
      cwd: '/home/rds9/bots/worldwarbot',
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      env: {
        PYTHONUNBUFFERED: '1',
        PATH: '/home/rds9/.local/bin:/usr/local/bin:/usr/bin:/bin'
      }
    }
  ]
};

const app = require('./app');
const { initializeDatabase } = require('./database/init_db');

const host = process.env.HOST || '0.0.0.0';
const port = Number(process.env.PORT || 3000);

// 先完成数据库初始化再监听端口，避免首个请求撞上初始化竞态。
initializeDatabase()
  .then(() => {
    app.listen(port, host, () => {
      console.log(`Backend listening at http://${host}:${port}`);
    });
  })
  .catch((error) => {
    console.error('Database initialization failed:', error);
    process.exitCode = 1;
  });

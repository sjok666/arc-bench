const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');

const app = express();

app.use(cors());
app.use(bodyParser.json({ limit: '2mb' }));

// 通用健康检查：Runner 用它判断应用是否已经就绪。
app.get('/api/health', (req, res) => {
  res.json({ code: 200, message: 'Backend Ready' });
});

// 通用静态托管：产出 frontend/dist 后由后端统一对外提供页面与前端资源。
const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');

if (fs.existsSync(frontendDistPath)) {
  app.use(express.static(frontendDistPath));
  app.get(/^(?!\/api(?:\/|$)).*/, (req, res) => {
    // 必须传 root, 不能传绝对路径: send 会逐段判定 dotfile,
    // 部署目录若含 . 开头的目录(如 .arc)会把 index.html 误判成 dotfile 而 404。
    res.sendFile('index.html', { root: frontendDistPath });
  });
} else {
  app.get('/', (req, res) => {
    res.status(503).send('Frontend build missing');
  });
}

module.exports = app;

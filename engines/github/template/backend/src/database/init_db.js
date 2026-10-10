const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();

// 通用 SQLite 连接封装：只负责建立连接与提供 Promise 化的读写助手，
// 不预置任何业务表结构。
let db = null;
let initPromise = null;

const databasePath = path.resolve(
  process.cwd(),
  process.env.ARC_DB_FILE || process.env.DATABASE_FILE || 'database.db',
);

function getDb() {
  if (!db) {
    fs.mkdirSync(path.dirname(databasePath), { recursive: true });
    db = new sqlite3.Database(databasePath);
  }
  return db;
}

function run(sql, params = []) {
  return new Promise((resolve, reject) => {
    getDb().run(sql, params, function onRun(error) {
      if (error) {
        reject(error);
        return;
      }
      resolve({ lastID: this.lastID, changes: this.changes });
    });
  });
}

function get(sql, params = []) {
  return new Promise((resolve, reject) => {
    getDb().get(sql, params, (error, row) => (error ? reject(error) : resolve(row)));
  });
}

function all(sql, params = []) {
  return new Promise((resolve, reject) => {
    getDb().all(sql, params, (error, rows) => (error ? reject(error) : resolve(rows)));
  });
}

function exec(sql) {
  return new Promise((resolve, reject) => {
    getDb().exec(sql, (error) => (error ? reject(error) : resolve()));
  });
}

async function initializeDatabase() {
  if (!initPromise) {
    initPromise = (async () => {
      await exec('PRAGMA foreign_keys = ON;');
    })();
  }
  return initPromise;
}

module.exports = { getDb, run, get, all, exec, initializeDatabase };

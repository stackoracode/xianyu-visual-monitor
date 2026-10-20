-- Run as a MySQL administrator. Replace the password placeholder first.
CREATE DATABASE IF NOT EXISTS xianyu_monitor CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'xvm'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
CREATE USER IF NOT EXISTS 'xvm'@'localhost' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
  ON xianyu_monitor.* TO 'xvm'@'127.0.0.1', 'xvm'@'localhost';
FLUSH PRIVILEGES;

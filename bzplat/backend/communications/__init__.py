"""平台通信内核。

普通平台通信以 conversation/message 为真相，邮件与旧 notifications 都是投影。
验证码和密码重置码使用不落消息正文的 transactional delivery。为避免 mail/store
初始化环，这里刻意不做 eager re-export；调用方从具体子模块导入。
"""

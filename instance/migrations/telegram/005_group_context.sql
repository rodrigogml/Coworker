ALTER TABLE messages ADD COLUMN sender_user_id INTEGER;
ALTER TABLE messages ADD COLUMN telegram_message_thread_id INTEGER;
ALTER TABLE messages ADD COLUMN chat_type TEXT;
CREATE INDEX messages_group_topic ON messages(chat_id, telegram_message_thread_id, message_id);

# Scheduler da instância

O scheduler é uma feature independente do Telegram. O gateway é o orquestrador da
instância: ele pode controlar o scheduler, o Telegram e outras interfaces futuras.
Uma tarefa não precisa de grupo, tópico ou Bot API para existir ou ser executada.

O núcleo está em `scheduler.py` e o banco privado em
`data/scheduler/scheduler.sqlite3`. O Telegram fornece somente um adaptador opcional
de notificação e de contexto conversacional.

Consulta e configuração local, sem credenciais Telegram:

```powershell
python scripts/scheduler_cli.py status
python scripts/scheduler_cli.py list
python scripts/scheduler_cli.py enable TASK_UID
python scripts/scheduler_cli.py disable TASK_UID
```

Jobs, execuções e estado do scheduler ficam em `instance/data/`. Scripts privados e
arquivos de suporte devem ficar em `instance/data/work/`; pontos de entrada públicos
em `interfaces/` e `skills/` também podem ser usados. Não criar estado, logs, bancos,
CODEX_HOME ou cópias de código em AppData, no perfil do usuário ou em qualquer
diretório externo.

# Comandos BIS10

O wrapper traduz as operações para o formato do BIS10CMD:

```text
-connect -ping
-connect -session
-connect -accountStatement create accountId <id> categoryId <id> date <ISO> value <valor> [displayLine <texto>] [notes <texto>] [audited true|false] confirm
-connect -accountStatement createTransfer debitAccountId <id> creditAccountId <id> date <ISO> value <valor> [displayLine <texto>] [notes <texto>] [audited true|false] confirm
-connect -accountStatement update id <id> [accountId <id>] [categoryId <id>] [date <ISO>] [value <valor>] [displayLine <texto>] [notes <texto>] [audited true|false] confirm
-connect -accountStatement updateTransfer id <id> [debitAccountId <id>] [creditAccountId <id>] [date <ISO>] [value <valor>] [displayLine <texto>] [notes <texto>] [audited true|false] confirm
-connect -accountStatement delete id <id> confirm
```

Datas aceitam `YYYY-MM-DD` ou `YYYY-MM-DDTHH:MM:SS`. Valores devem ser positivos.

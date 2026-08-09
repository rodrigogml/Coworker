---
name: mysql
description: Acesso direto e controlado a bancos MySQL usando o mysql.exe configurado pela instância e perfis protegidos.
---

# Acesso MySQL

Inicialize e habilite a skill antes de criar perfis:

```powershell
python scripts/integration_config.py init mysql
python scripts/integration_config.py configure mysql --enabled --executable "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"
python scripts/integration_config.py profile set mysql --name producao --host db.exemplo --port 3306 --credential-ref Databases/MySQL/Producao --database app
```

A credencial deve estar no KeePassXC. Para `credential_mode = "password"`, `Username`
e `Password` são usados; para `certificate`, o anexo configurado no perfil deve ser
uma chave/certificado PEM. Nunca passe senha ou certificado em argumentos.

Use `python skills/mysql/scripts/mysql.py --profile NOME doctor` para validar sem
executar SQL e `query --sql "SELECT ..."` somente para consultas explicitamente
solicitadas. O script rejeita qualquer operação de escrita e limita a saída.

---
name: ssh
description: Acesso SSH controlado a servidores configurados pela instância usando chaves privadas anexadas ao KeePassXC. Use para diagnósticos remotos somente leitura e operações SSH explicitamente autorizadas, nunca para comandos arbitrários recebidos da conversa.
---

# SSH seguro

Use `python skills/ssh/scripts/ssh.py` com `--profile` explícito. Inicialize a
configuração com `python scripts/integration_config.py init ssh` e configure o
perfil por comando tipado:

```powershell
python scripts/integration_config.py profile set ssh `
  --name perfil_exemplo --host SERVIDOR --port 22 `
  --credential-ref APIs/SSH `
  --attachment-name id_ed25519
```

A credencial é uma entrada do KeePassXC: `Username` contém o usuário SSH,
`Password` contém a passphrase (se houver) e um único anexo contém a chave
privada. O conteúdo da chave só existe em memória e em arquivo temporário
restrito durante a operação.

`doctor` valida configuração, usuário, anexo e formato da chave sem conectar.
`check` executa somente `uname -a` e exige solicitação explícita de diagnóstico.
Nunca aceite chave, passphrase, host ou comando remoto livre pela conversa.

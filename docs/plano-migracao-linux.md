# Plano de migração e reconciliação com Linux

Status: diagnóstico inicial no branch `linux-conciliation`  
Data: 2026-08-10  
Base: commit `a8fc472` (`origin/main`)

## 1. Resultado executivo

O núcleo Python é majoritariamente portável: usa `pathlib`, SQLite, biblioteca
padrão HTTP/JSON e subprocessos sem shell. A migração, porém, exige adaptadores
de sistema operacional. Os bloqueios de primeira ordem são:

- o cofre depende de `Advapi32.dll` e do Windows Credential Manager para guardar a
  senha mestra do KeePassXC;
- o serviço usa `pywin32` e o Service Control Manager; Linux ainda é declarado
  como MVP futuro;
- o bootstrap assume convenções Windows (`KeePassXC.exe`, `keepassxc-cli.exe`,
  `ProgramFiles`, `LOCALAPPDATA` e `.venv/Scripts/python.exe`);
- BIS2, MySQL, Codex CLI, EccoVox, navegador e KeePassXC são aplicações externas
  que não estão instaladas por `requirements.txt`.

A sequência segura é portar instalação, cofre e supervisor primeiro; depois
validar integrações locais e, por último, APIs externas com perfis reais. Não
copiar `instance/data/` para Git nem mover segredos para arquivos permanentes,
argumentos ou diretórios fora da instância.

## 2. Estado e linha de base

- O clone local estava limpo, mas `main` estava 10 commits atrás de `origin/main`.
- Foi executado `git fetch --prune origin`.
- `linux-conciliation` foi criado sobre `origin/main`, está ativo e iniciou limpo
  no commit `a8fc472`.
- Não havia branch local preexistente com esse nome.
- A suíte contém 430 testes coletáveis no ambiente atual.
- Não há `pyproject.toml`, `setup.py`, lockfile, Dockerfile ou Conda versionado;
  a especificação Python é `instance/requirements.txt`.

## 3. Inventário da aplicação

### Estrutura pública

- `instance/interfaces/telegram/`: gateway, polling, estado, processamento de
  arquivos, captura de credenciais, automações, transcrição e adaptador Codex.
- `instance/skills/`: 13 skills (`bis2`, `calendar`, `cloudflare`, `contacts`,
  `cpfl`, `drive`, `forwardemail`, `gmail`, `mysql`, `notion`, `omie`, `ssh`,
  `todoist` e `totp`), cada uma com `SKILL.md` e scripts/referências quando
  aplicável.
- `instance/scripts/`: instalação, cofre, OAuth Google, memória, bancos, logs,
  configuração e serviço.
- `instance/migrations/`: schemas versionados da memória e Telegram.
- `instance/config/` e `instance/templates/`: modelos públicos sem segredos.
- `tests/`: testes que importam diretamente o runtime de `instance/`.
- `install.sh` e `install.ps1`: entradas de instalação; o primeiro já escolhe
  Python 3, o segundo oferece opções de serviço Windows.
- `docs/`: documentação de desenvolvimento deste branch, fora do runtime para
  respeitar a proibição de `instance/docs/`.

### Dados e isolamento

Configuração privada, SQLite, estado Telegram, `CODEX_HOME`, logs, mídias e
artefatos devem permanecer em `instance/data/`, ignorado pelo Git. A migração
deve preservar a raiz lógica mesmo que o caminho Linux seja `/home/...`.

Validar explicitamente: permissões de `data/`; umask; links simbólicos e hard
links; WAL/journal SQLite; UTF-8 e Unicode; nomes case-sensitive; e ausência de
caches, logs ou `CODEX_HOME` no `$HOME` fora de `instance/data/`.

## 4. Dependências Python atuais

| Pacote | Versão/uso | Atenção Linux |
|---|---|---|
| `pykeepass` | `4.1.1.post1`, KDBX | instalável, mas não implementa o backend da senha mestra |
| `pypdf` | `6.14.2`, extração PDF | portável; validar PDFs reais e limites |
| `zxing-cpp` | `2.3.0`, QR/TOTP | pode exigir wheel compatível, compilador ou headers |
| `pywin32` | `>=306`, somente Windows | não substitui `systemd` |

Não há versão mínima de Python, matriz de distribuição/arquitetura, hashes de
wheels ou lockfile. Definir isso antes da implementação; usar venv e não instalar
pacotes globais.

## 5. Integrações e aplicações externas

### Skills

- **BIS2:** Java + JAR BISCMD, servidor BIS2/WildFly, host/porta, TLS/truststore,
  encoding e credenciais KeePassXC.
- **Google (Calendar, Contacts, Drive, Gmail):** OAuth local, navegador na
  máquina Linux, callback em `127.0.0.1`, CA/TLS, relógio/fuso e rede.
- **Cloudflare, Forward Email, Notion, Omie e Todoist:** APIs HTTPS, perfis,
  tokens no cofre, DNS/CA/proxy/timeouts e rate limits; Omie tem operações
  financeiras controladas.
- **CPFL:** link oficial individual, CPF protegido, PDF, navegador e CAPTCHA
  humano; não há bypass via HTTP.
- **MySQL:** cliente nativo `mysql`/`mysql.exe`, TLS e possível certificado PEM
  temporário com permissões restritas.
- **SSH:** cliente `ssh`, permissões de chave temporária, host keys e servidor
  remoto.
- **TOTP:** QR por `zxing-cpp` e acesso ao cofre.

As APIs usam principalmente `urllib`, sem SDKs Python adicionais. Isso reduz
dependências, mas exige testar CA bundle, proxy, IPv6/DNS, locale, TLS e timeouts
da distribuição Linux.

### Componentes fora das skills

- **KeePassXC/CLI:** indispensável para o KDBX. É necessário escolher backend
  Linux seguro para a senha mestra (por exemplo Secret Service/libsecret ou
  desbloqueio interativo). Arquivo de senha não é fallback aceitável.
- **Codex CLI:** subprocessos isolados com `CODEX_HOME`, regras, permissões e
  workspace; verificar binário, autenticação, modelo e compatibilidade da CLI.
- **Java/BISCMD:** validar versão Java, PATH, JAR, truststore, encoding e servidor.
- **Cliente MySQL:** validar executável, argumentos, TLS, locale e saída Linux.
- **EccoVox:** CLI ou endpoint HTTP opcional; validar binário, porta localhost,
  modelo, CPU/memória, permissões e HTTPS no modo remoto.
- **Navegador:** necessário para OAuth e CPFL; validar X11/Wayland, navegador
  padrão, sessão sem display e interação humana no CAPTCHA.

## 6. Bloqueios e adaptações

### Decisão arquitetural registrada

Foi decidido que o gateway Linux poderá acessar automaticamente o cofre após
cada reinicialização. Isso não autoriza guardar a senha mestra em arquivo comum,
variável de ambiente, argumento de processo ou dentro do repositório.

O desenho de referência passa a ser:

- cofre operacional da instância separado do cofre pessoal sempre que possível;
- serviço `systemd` de sistema executado por usuário Linux dedicado;
- credencial de desbloqueio provisionada por mecanismo protegido do `systemd`,
  com avaliação de TPM/LUKS ou backend externo conforme o modelo de ameaça;
- disco do servidor protegido por LUKS e permissões mínimas na árvore
  `instance/data/`;
- modo manual preservado para manutenção, rotação e recuperação;
- logs, diagnósticos e subprocessos sem exposição da senha mestra.

O backend Windows Credential Manager continuará sendo mantido para Windows. A
implementação Linux deve compartilhar um contrato de backend e seus testes, mas
não forçar uma solução específica para os dois sistemas.

### Primeiro incremento implementado

O branch já contém a base executável para esse desenho:

- `credential_vault.py` aceita uma seção `[linux_credential]` e lê a credencial
  efêmera entregue em `CREDENTIALS_DIRECTORY` pelo `systemd`;
- o comando `enroll` Linux valida a senha contra o KDBX e chama
  `systemd-creds encrypt`, removendo o plaintext temporário após o provisionamento;
- o CLI KeePassXC é suficiente em servidor headless; a GUI ficou opcional no Linux;
- `scripts/systemd_service.py` gera uma unidade com
  `LoadCredentialEncrypted=`, usuário dedicado, `NoNewPrivileges`,
  `ProtectHome`, `ProtectSystem` e `ReadWritePaths` restrito a `instance/data`;
- o instalador seleciona o supervisor nativo por plataforma e preserva o módulo
  Windows existente;
- testes de contrato cobrem credencial Linux, permissões e unidade `systemd`.

O provisionamento real ainda depende de executar o fluxo no Debian alvo, pois o
ciphertext de `systemd-creds` é vinculado ao host/TPM conforme a configuração local
do sistema. O arquivo `.cred` não deve ser copiado para outra máquina sem novo
provisionamento.

Para instalar o serviço no Debian, o fluxo exige `--service-user USUARIO` e nunca
aceita instalar o gateway como `root`. A criação da conta Linux e a concessão das
permissões mínimas continuam sendo etapas do provisionamento da máquina, não do
runtime Python.

### P0 — cofre

`instance/scripts/credential_vault.py` usa `ctypes`, `Advapi32.dll`,
`CredReadW`/`CredWriteW` e Windows Credential Manager. Em Linux, o backend atual
falha deliberadamente; logo todas as skills que leem KeePassXC ficam bloqueadas.

Implementar uma interface de backend, manter a implementação Windows, adicionar
backend Linux explícito para desbloqueio automático headless, e cobrir `enroll`,
`unenroll`, `status`, leitura, escrita, anexos, concorrência, rotação e limpeza.
Migrar apenas os KDBX autorizados e provisionar novamente o desbloqueio; nunca
materializar a senha em log, argumento, variável de ambiente ou documentação.

### P0 — serviço

`windows_service.py` exige Windows/`pywin32`; não existe unidade equivalente.
Criar adaptador `systemd` com unidade por instância, usuário dedicado,
`WorkingDirectory`, `ExecStart`, `CODEX_HOME` privado, `Restart`, sinais,
prevenção de duplicidade, status estruturado, logs definidos e instalação/remoção
reversível. Decidir entre `systemd --user` e serviço de sistema, inclusive
comportamento sem sessão gráfica e acesso ao Secret Service. Não usar shell
genérico nem espalhar condicionais Windows pelo runtime.

### P1 — bootstrap e paths

Extrair módulo de plataforma para descoberta de Python, Codex, KeePassXC, SSH,
MySQL, Java e EccoVox; nomes de executáveis; caminhos; browser; permissões e
subprocessos. Eliminar do fluxo comum referências a `ProgramFiles`,
`LOCALAPPDATA`, `.venv/Scripts`, `.exe` e mensagens Windows, preservando Windows
por adaptador e mantendo os testes existentes.

### P1 — processos e segurança

Auditar cada `subprocess.run`/`Popen`: `shell=False`, listas de argumentos,
encoding, ambiente herdado, sinais, grupos de processo, timeout e códigos de
saída. No Linux validar `SIGTERM`/`SIGKILL`, encerramento de árvores Codex,
permissões `0600`, umask, limites de arquivos/memória/processos e ausência de
injeção por nomes, perfis, SQL, paths e entradas externas.

## 7. Estratégia por fases

### Fase 0 — contrato e matriz

Fixar distribuição, arquitetura, versão mínima de Python, modo headless, desktop
e supervisor. Criar matriz Windows/Linux e checklist de comandos externos. Definir
política de segredo e backup antes de tocar em uma instância real.

### Fase 1 — núcleo portátil

Separar plataforma, revisar paths e subprocessos, criar venv Linux, documentar
instalação sem alterar o sistema global e garantir que os testes rodem sem rede
ou credenciais reais.

### Fase 2 — cofre

Implementar o backend Linux do KeePassXC. Validar consultas, escritas, atributos,
anexos, bloqueio da GUI, concorrência e recuperação após reinício. Só prosseguir
quando cofre, entidades, TOTP e captura Telegram passarem no Linux.

### Fase 3 — gateway e supervisor

Portar descoberta Codex, sinais, logging e `systemd`. Testar start/stop/restart,
processo órfão, duplicidade, reboot e serviço sem terminal. Confirmar que nenhum
estado aparece fora de `instance/data/`.

### Fase 4 — integrações locais

Validar SQLite/memória, Telegram sem e com mídia, Codex CLI, SSH, MySQL, TOTP,
PDF/QR, EccoVox, BIS2 e CPFL, nessa ordem. Cada item precisa de diagnóstico e
stub antes de credenciais reais.

### Fase 5 — APIs externas

Reautorizar Google OAuth na máquina Linux e executar leituras por perfil. Depois
validar Cloudflare, Forward Email, Todoist, Notion e Omie. Mutações exigem perfil,
`--dry-run` quando disponível e autorização separada; a migração não autoriza
efeitos externos.

### Fase 6 — dados e operação

Fazer backup verificável, parar o gateway, migrar somente `instance/data/`, conferir
hash, tamanho e permissões, executar migrations, validar cofre/configuração sem
imprimir segredos e iniciar o serviço Linux. Manter rollback para Windows durante
o período de observação.

## 8. Critérios de aceite

- instalação limpa, repetível e documentada em venv Linux;
- `python -m pytest` verde no Linux, com testes específicos de caminhos Linux;
- nenhum caminho de execução Linux importa `pywin32` ou chama WinDLL;
- cofre funcional sem senha mestra em texto puro e com erros acionáveis;
- gateway/scheduler sobrevivem a restart e reboot sem duplicar processos/jobs;
- todos os arquivos persistentes continuam em `instance/data/`;
- Codex, Telegram, SSH, MySQL, Java/BISCMD, EccoVox, navegador e APIs têm
  diagnóstico individual e dependências verificadas;
- backup, restauração e SQLite em WAL foram exercitados;
- logs e respostas continuam sanitizados;
- Windows mantém o fluxo atual de serviço e seus testes.

## 9. Checklist de instalação Linux

- [ ] escolher distribuição, arquitetura, Python e supervisor;
- [ ] instalar Python/venv, KeePassXC, cliente SSH, cliente MySQL e Java por
      pacotes oficiais ou fontes aprovadas;
- [ ] instalar/configurar Codex CLI, EccoVox e navegador conforme necessidade;
- [ ] criar backup offline/verificado de `instance/data/`;
- [ ] preparar backend seguro da senha mestra e confirmar bloqueio da GUI durante
      escritas;
- [ ] definir usuário/grupo Linux e permissões da instância;
- [ ] configurar DNS, CA, proxy, firewall e acesso aos endpoints;
- [ ] executar `doctor` de cada integração sem mutações;
- [ ] validar OAuth local e o modo headless escolhido;
- [ ] simular parada, reinício, falha de rede, expiração de credencial e reboot;
- [ ] registrar versões/caminhos efetivos apenas no inventário privado.

## 10. Riscos que não devem ser mascarados

- Python portável não implica cofre, supervisor ou executáveis portáveis.
- Trocar `Advapi32.dll` por arquivo de senha local seria regressão de segurança.
- OAuth pode falhar em servidor sem display; isso exige fluxo assistido ou desenho
  alternativo aprovado.
- Java/BISCMD, MySQL e EccoVox podem ter incompatibilidade de arquitetura, versão,
  encoding ou licenciamento que pip não resolve.
- Usuário/grupo, umask e permissões do serviço exigem matriz de execução Linux.
- Os 430 testes unitários não substituem smoke tests de APIs, navegador, cofre,
  binários e serviços no ambiente Linux alvo.

## 11. Próximos artefatos recomendados

1. Pacote de plataforma com contratos Windows/Linux.
2. Backend Linux do cofre e testes de contrato compartilhados.
3. Adaptador `systemd` com template de unidade e fluxo reversível.
4. Matriz de compatibilidade e lockfile/manifesto do ambiente Linux.
5. `doctor` para Codex, KeePassXC, Java/BISCMD, MySQL, SSH e EccoVox.
6. Teste de migração/rollback de `instance/data/`, incluindo SQLite WAL.
7. CI Linux sem segredos e smoke tests isolados para rede, navegador e aplicações
   proprietárias.

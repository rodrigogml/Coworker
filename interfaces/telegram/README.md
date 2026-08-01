# Interface Telegram

As respostas do Codex são normalizadas na saída: Markdown comum é convertido para o
subconjunto HTML aceito pelo Telegram, com escape obrigatório do conteúdo. Títulos,
ênfases, listas, links, citações e código preservam formatação compatível; links para
caminhos locais são apresentados como texto, pois não são acessíveis pelo aplicativo.

Esta aplicação conecta uma conversa particular do Telegram a sessões não interativas
do Codex CLI. Ela é uma interface da BOTina, não uma skill: recebe mensagens e mídias,
controla autorização, mantém uma fila local e devolve a resposta final do Codex.

## Limites atuais

- somente uma pessoa proprietária;
- somente conversa particular;
- uma execução Codex por vez;
- long polling como transporte operacional;
- textos, documentos, fotos, áudio, voz e vídeo;
- respostas nativas, mensagens referenciadas e álbuns de entrada;
- artefatos de saída como foto, documento, áudio, voz, vídeo ou animação;
- webhook reservado na configuração, ainda sem servidor ativado;
- uma única máquina deve executar o bot por vez.

O token, a pessoa autorizada, o estado, as mensagens e os arquivos recebidos são dados
locais e não são versionados. O modelo público contém apenas nomes de campos e valores
seguros.

## Preparação

1. Crie um bot usando o BotFather e mantenha desabilitada sua participação em grupos.
2. Inicialize a configuração sem sobrescrever uma existente:

   ```powershell
   python interfaces/telegram/botina_telegram.py init
   ```

3. Cadastre o token na entrada `APIs/Telegram/BOTina` do KeePassXC:

   ```powershell
   python scripts/credential_vault.py add "APIs/Telegram/BOTina"
   ```

4. Instale e autentique uma distribuição autônoma do Codex CLI. O executável interno
   do aplicativo desktop não deve ser presumido como acessível a processos externos.
5. Se o comando `codex` não estiver no `PATH`, registre o caminho comprovado no campo
   `codex.executable` de `data/config/telegram.toml`.
6. O campo `codex.home_dir` identifica a instância isolada usada pelo gateway. Quando
   vazio, assume `%LOCALAPPDATA%\BOTina\codex`; não deve apontar para o diretório do
   Codex Desktop.
7. Mantenha `codex.network_access = false` até decidir habilitar integrações externas.
   Para Gmail, Notion e outras APIs, altere-o para `true` após autorização explícita.
8. Execute o diagnóstico:

   ```powershell
   python interfaces/telegram/botina_telegram.py doctor
   ```

O backend padrão continua sendo `exec`. Para ativar gradualmente o App Server nesta
instalação, altere manualmente apenas o arquivo privado:

```toml
[codex]
backend = "app-server"
```

Depois reinicie o processo e execute `doctor`. A interface não troca esse valor nem
reescreve configurações privadas. Ambos os backends usam o mesmo schema de entrega;
`exec` usa `--output-schema`, enquanto o App Server usa `turn/start.outputSchema`.

O gateway mantém as regras de execução do `config/codex-botina.rules` sincronizadas
em `<codex.home_dir>/rules/botina.rules`. Elas liberam comandos de leitura e os pontos
de entrada públicos das integrações, mantendo comandos arbitrários sujeitos à política
do Codex. A sincronização ocorre ao iniciar o polling e também pode ser administrada
localmente:

```powershell
python interfaces/telegram/botina_telegram.py permissions status
python interfaces/telegram/botina_telegram.py permissions sync
```

Ao iniciar o polling, a aplicação publica automaticamente no Telegram o menu de
comandos válidos para conversas particulares. Para consultar ou reaplicar essa
configuração sem reiniciar o gateway, use:

```powershell
python interfaces/telegram/botina_telegram.py commands status
python interfaces/telegram/botina_telegram.py commands sync
```

A lista publicada e a resposta de `/help` usam a mesma definição no código. Assim,
uma instalação nova precisa apenas ter o token cadastrado antes da primeira execução.

## Vinculação inicial

Abra o polling em um terminal:

```powershell
python interfaces/telegram/botina_telegram.py run
```

Em outro terminal, gere um PIN temporário:

```powershell
python interfaces/telegram/botina_telegram.py pairing begin
```

Envie `/pair 123456` na conversa particular, usando o PIN realmente apresentado. A
mensagem valida o PIN, mas ainda não autoriza a conta. Consulte localmente a solicitação:

```powershell
python interfaces/telegram/botina_telegram.py pairing status
```

Confira nome, username, `user_id` e `chat_id`. Somente então use o `approval_code`:

```powershell
python interfaces/telegram/botina_telegram.py pairing approve ABC123
```

O PIN possui seis dígitos, é armazenado somente como PBKDF2-HMAC, expira, aceita um
número limitado de tentativas e é invalidado depois do uso. A aprovação local cria a
raiz de confiança da instalação.

## Conversa

- `/new`: desvincula a sessão atual; não apaga o histórico do Codex;
- `/resume`: quando enviado como resposta a uma mensagem anterior da BOTina, torna a
  thread daquela mensagem explicitamente ativa;
- `/status`: mostra sessão e fila;
- `/usage`: consulta as janelas de franquia da conta autenticada no Codex;
- `/cancel`: solicita o encerramento da execução ativa;
- `/thread`: mostra o identificador da sessão;
- `/help`: lista os comandos.

Responder a uma mensagem inclui um único nível de contexto, quote e anexos conhecidos,
sem trocar a thread ativa. A confirmação, a resposta final e o primeiro artefato usam
`reply_parameters` para apontar nativamente à solicitação. IDs devolvidos pelo Telegram,
thread e turn são persistidos no SQLite.

No backend `exec`, o gateway usa `codex exec --json` e `codex exec resume`. No backend
`app-server`, inicia um transporte stdio, faz `initialize`/`initialized`, cria ou retoma
a thread, usa `localImage` para imagens e encerra somente em `turn/completed`. Apenas o
`agentMessage` final é publicado; raciocínio, logs, comandos e saída de ferramentas não
são retransmitidos. `/cancel` termina o processo `exec` ou envia `turn/interrupt` ao
App Server.

## Trabalhos e artefatos

Cada solicitação cria uma caixa isolada em `data/telegram/jobs/<job-id>/`:

```text
input/                 arquivos atuais e referenciados
derived/               conteúdo preparado pelos processadores
output/                únicos arquivos elegíveis para envio
delivery-schema.json   contrato fornecido ao Codex
result.json            resposta estruturada original
```

Um artefato declarado precisa ser relativo a `output/`, regular, não vazio, estar sob
o limite de upload e permanecer dentro da pasta após `Path.resolve(strict=True)`.
Links, junctions e reparse points que escapem da saída são rejeitados. O gateway
detecta MIME por assinatura quando possível, calcula SHA-256 e escolhe o método nativo:
JPEG/WebP e PNG sem alpha como foto; PNG com alpha e arquivos genéricos como documento;
GIF como animação; áudio convencional como áudio; OGG como voz; vídeo como vídeo.

O publicador `interfaces/telegram/scripts/publish_artifact.py` é o único comando de
cópia liberado ao Codex. Ele recebe o destino somente por `BOTINA_JOB_OUTPUT`, não
sobrescreve arquivos e devolve apenas metadados seguros. Arquivos em
`<CODEX_HOME>/generated_images` são concedidos somente para leitura.

## Processadores

Sem dependências externas, a interface prepara texto UTF-8, JSON, CSV, XML, inventário
de ZIP e extração básica de DOCX/XLSX por XML interno. ZIP possui limites de membros e
tamanho descompactado e rejeita path traversal. Imagens seguem como entrada visual
nativa. PDF, OCR, áudio e vídeo possuem diagnóstico opcional; quando a dependência não
está disponível, o original é preservado e essa limitação é informada ao Codex. A
interface nunca instala processadores automaticamente nem executa arquivos recebidos.

Os limites ficam em `[processors]` e `[media]` no TOML privado. A retenção dos jobs é
manual nesta versão: remova caixas antigas somente com o gateway parado e depois de
confirmar que seus artefatos não são mais necessários.

`/usage` usa `account/rateLimits/read` do Codex App Server. A resposta pode incluir
limites primário e secundário, percentuais disponíveis, horários de renovação, plano
e créditos adicionais quando esses campos forem fornecidos pela conta. Essa consulta
não cria uma sessão Codex e não consome uma solicitação ao modelo.

Cada processo recebe `CODEX_HOME` exclusivamente pelo ambiente filho. Assim, login,
configuração, sessões, logs e estado do gateway permanecem separados do Codex Desktop
sem alterar variáveis globais do Windows.

## Dados e segurança

Por padrão, o SQLite operacional fica em `%LOCALAPPDATA%\BOTina\telegram`. Isso evita
sincronização concorrente de um banco ativo. Entradas, derivados e saídas ficam em
`data/telegram/jobs/`, com metadados, tamanho e SHA-256, e permanecem ignorados pelo Git.

A autorização usa os IDs numéricos do usuário e da conversa, nunca o `@username`.
Mensagens de grupos, usuários não autorizados e tipos desconhecidos não chegam ao
Codex. Arquivos recebidos são tratados como conteúdo não confiável e nunca são
executados pelo gateway.

Use `workspace-write` como sandbox inicial. O gateway converte essa opção em um perfil
de permissões explícito do Codex, com escrita somente nas raízes do workspace. O campo
`codex.network_access` controla separadamente a rede dos comandos executados. Libere
outros diretórios individualmente em `codex.additional_directories`; não use
`danger-full-access` para compensar uma configuração incompleta.

Com `approval_policy = "never"`, um comando que não corresponda às regras públicas é
recusado em vez de solicitar aprovação pela conversa. Ao criar um novo script que a
interface deva executar, adicione seu ponto de entrada ao modelo de regras, sem liberar
o interpretador Python ou o PowerShell de forma genérica.

## Estado e recuperação

```powershell
python interfaces/telegram/botina_telegram.py status
python interfaces/telegram/botina_telegram.py pairing cancel
```

O `update_id` do Telegram é persistido antes do processamento para impedir execução
duplicada. Uma sessão só é substituída depois que o Codex devolve um novo
`thread_id`. Falhas não revelam token, senha mestra nem saída bruta do processo.

Ao reiniciar, jobs que estavam na fila ou executando são marcados como falhos. Um
artefato que estava em `uploading` passa para `unknown`: ele não é reenviado
automaticamente, pois a Bot API pode ter aceitado o upload antes da queda e não oferece
uma chave geral de idempotência para essa operação.

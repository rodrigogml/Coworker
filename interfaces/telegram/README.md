# Interface Telegram

As respostas do Codex são normalizadas na saída: Markdown comum é convertido para o
subconjunto HTML aceito pelo Telegram, com escape obrigatório do conteúdo. Títulos,
ênfases, listas, links, citações e código preservam formatação compatível; links para
caminhos locais são apresentados como texto, pois não são acessíveis pelo aplicativo.

Esta aplicação conecta uma conversa particular do Telegram a sessões não interativas
do Codex CLI. Ela é uma interface da Coworker, não uma skill: recebe mensagens e mídias,
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

1. Execute o instalador da raiz. Ele coleta a identidade e cria a configuração sem
   sobrescrever arquivos existentes:

   ```powershell
   .\install.ps1
   ```

2. Crie um bot usando o BotFather e mantenha desabilitada sua participação em grupos.
   O `@username` é definido no BotFather e pode ser diferente do nome da instância.
3. O instalador cadastra o token na referência específica da instância, com o formato:

   ```text
   APIs/Telegram/<instance_id>
   ```

4. O próprio instalador publica nome, descrições e comandos, abre o pareamento,
   apresenta os IDs numéricos para confirmação local e inicia o gateway. A vinculação
   continua exigindo confirmação humana na máquina; validar o PIN no Telegram não é
   suficiente.

5. No menu do configurador, abra **Codex CLI, CODEX_HOME e permissões**. O Codex CLI é
   obrigatório: a seção informa se o executável foi localizado, mostra sua versão,
   cria o `CODEX_HOME` exclusivo da instância e permite autenticar a conta isolada.
   O executável interno do aplicativo desktop não deve ser presumido como acessível a
   processos externos.
6. Se o comando `codex` não estiver no `PATH`, informe o caminho comprovado nessa
   seção. O valor é registrado em `codex.executable`.
7. O campo `codex.home_dir` identifica a instância isolada usada pelo gateway. O
   configurador grava explicitamente o padrão
   `%LOCALAPPDATA%\Coworker\instances\<instance_id>\codex`; ele não deve apontar para o
   diretório do Codex Desktop.
8. Mantenha `codex.network_access = false` até decidir habilitar integrações externas.
   Para Gmail, Notion e outras APIs, altere-o para `true` após autorização explícita.
9. Execute o diagnóstico:

   ```powershell
   python interfaces/telegram/gateway.py doctor
   ```

O instalador usa o App Server como backend inicial, mantendo `exec` disponível como
fallback. Para selecionar explicitamente o fallback, altere somente o arquivo privado:

```toml
[codex]
backend = "exec"
model = ""
reasoning_effort = ""
speed = "standard"
verbosity = ""
```

Os quatro últimos campos são padrões opcionais da instância. Valores vazios herdam a
seleção da conta ou do modelo. Depois reinicie o processo e execute `doctor`. A
interface não troca o backend nem reescreve configurações privadas. Ambos os backends
usam o mesmo schema de entrega;
`exec` usa `--output-schema`, enquanto o App Server usa `turn/start.outputSchema`.

O gateway mantém as regras de execução do `config/codex.rules` sincronizadas
em `<codex.home_dir>/rules/gateway.rules`. Elas liberam comandos de leitura e os pontos
de entrada públicos das integrações, mantendo comandos arbitrários sujeitos à política
do Codex. A sincronização ocorre ao iniciar o polling e também pode ser administrada
localmente:

```powershell
python interfaces/telegram/gateway.py permissions status
python interfaces/telegram/gateway.py permissions sync
```

Ao iniciar o polling, a aplicação publica automaticamente no Telegram o menu de
comandos válidos, o nome e as descrições derivados de `data/config/identity.toml`.
O username não pode ser alterado pela Bot API. Para consultar ou reaplicar essas
configurações sem reiniciar o gateway, use:

```powershell
python interfaces/telegram/gateway.py commands status
python interfaces/telegram/gateway.py commands sync
python interfaces/telegram/gateway.py profile status
python interfaces/telegram/gateway.py profile sync
```

A lista publicada e a resposta de `/help` usam a mesma definição no código. Assim,
uma instalação nova precisa apenas ter o token cadastrado antes da primeira execução.

## Vinculação inicial

Abra o polling em um terminal:

```powershell
python interfaces/telegram/gateway.py run
```

O configurador local também administra o processo persistente. Execute
`python scripts/install_instance.py` e escolha **6. Gerenciar gateway Telegram**
para consultar o status, iniciar, finalizar ou reiniciar. O processo registra PID e
horário em `<state_dir>/gateway-runtime.json`; a finalização usa uma
solicitação cooperativa e espera o polling corrente terminar. O registro
também impede duas cópias gerenciadas da mesma instância.

A inicialização tenta sincronizar comandos, nome e descrições públicas, mas uma
limitação temporária dessas edições pela Bot API não impede mais o polling. Avisos e
falhas de inicialização ficam em `<state_dir>/gateway.log`; quando o processo encerrar
prematuramente, o configurador apresenta a última linha desse log. Respostas HTTP 429
incluem o tempo de espera informado pelo Telegram, sem revelar o token.

As opções de instalar e remover como serviço já aparecem reservadas no menu,
mas ainda não executam alterações no Windows. Um gateway iniciado antes desta
versão não possui registro persistente e deve ser finalizado manualmente uma vez
antes de passar ao novo gerenciador.

Em outro terminal, gere um PIN temporário:

```powershell
python interfaces/telegram/gateway.py pairing begin
```

Envie `/pair 123456` na conversa particular, usando o PIN realmente apresentado. A
mensagem valida o PIN, mas ainda não autoriza a conta. Consulte localmente a solicitação:

```powershell
python interfaces/telegram/gateway.py pairing status
```

Confira nome, username, `user_id` e `chat_id`. Somente então use o `approval_code`:

```powershell
python interfaces/telegram/gateway.py pairing approve ABC123
```

O PIN possui seis dígitos, é armazenado somente como PBKDF2-HMAC, expira, aceita um
número limitado de tentativas e é invalidado depois do uso. A aprovação local cria a
raiz de confiança da instalação.

## Conversa

- `/new`: desvincula a sessão atual; não apaga o histórico do Codex;
- `/resume`: quando enviado como resposta a uma mensagem anterior da Coworker, torna a
  thread daquela mensagem explicitamente ativa;
- `/settings` ou `/codex`: abre o painel de configuração do Codex;
- `/model [MODELO|default]`: lista ou seleciona um modelo anunciado pela conta;
- `/reasoning [NÍVEL|default]`: lista ou seleciona somente esforços compatíveis;
- `/speed [standard|fast]`: consulta ou altera a velocidade; Fast exige confirmação;
- `/verbosity [low|medium|high|default]`: controla o detalhamento da resposta;
- `/progress [off|compact|detailed]`: controla as atualizações provisórias durante
  o processamento;
- `/codex diagnose`: verifica CLI, autenticação, App Server e catálogo de modelos;
- `/codex reset`: restaura os padrões da instância após confirmação;
- `/status`: abre o painel de sessão e fila, com atualização, cancelamento, nova
  conversa, franquia e acesso às configurações;
- `/usage`: consulta as janelas de franquia da conta autenticada no Codex;
- `/cancel`: solicita o encerramento da execução ativa;
- `/thread`: mostra o identificador da sessão;
- `/secret NomeDoServico`: prepara a captura protegida da próxima mensagem de texto;
- `/help`: lista os comandos.

Os painéis usam teclados inline e editam a mesma mensagem durante a navegação. Todo
`callback_query` é interceptado antes do SQLite de mensagens, da fila e do Codex,
revalida `user_id` e `private_chat_id` e expira depois de quinze minutos. Os botões de
modelo carregam identificadores opacos; o valor é resolvido e validado novamente no
catálogo oficial `model/list` antes da gravação.

Preferências da conversa ficam no SQLite operacional e sobrevivem ao reinício do
gateway. Elas valem para as solicitações enviadas depois da alteração e continuam
ativas após `/new`. A precedência é preferência do Telegram, padrão em
`data/config/telegram.toml` e, por fim, padrão do Codex. `/codex reset` remove somente
as preferências do Telegram.

O painel de progresso oferece três modos. `off` mantém somente o indicador de
digitação e a resposta final; `compact` transmite marcos operacionais sanitizados;
`detailed` também transmite mensagens `commentary` destinadas à pessoa usuária e
mantém um resumo das etapas ao concluir. Instalações existentes permanecem em `off`
até uma escolha explícita. A preferência é fotografada quando a solicitação entra na
fila e não altera trabalhos já enfileirados.

Modelo, reasoning, velocidade e verbosity são parâmetros de inferência; progresso é
uma preferência de apresentação da interface. Sandbox,
rede, política de aprovação, diretórios graváveis, backend e modo super permanecem
somente no configurador local e não podem ser alterados por callback ou comando do
Telegram. O backend `exec` recebe opções tipadas por `--config`; o App Server recebe
`model`, `effort` e `serviceTier` por turno. Nenhuma interface aceita chaves de
configuração arbitrárias.

`/secret` reconhece deterministicamente aliases das integrações simples mantidas pelo
projeto. Por exemplo, `/secret atualize o token do Todoist` resolve para
`APIs/Todoist`; o gateway mostra o destino antes de preparar a captura. A mensagem
seguinte é interceptada antes de ser gravada no SQLite, incluída em um job ou enviada
ao Codex. O gateway salva o valor no campo `Password`, registra somente
`[Censurado por segurança]` e solicita `deleteMessage` à Bot API.

Os aliases conhecidos são Todoist, Notion, Cloudflare, Forward Email e Telegram. O
texto pode conter palavras auxiliares, mas deve identificar exatamente um serviço. Um
destino desconhecido é recusado; credenciais personalizadas continuam possíveis com
um caminho explícito, por exemplo `/secret APIs/ServicoPersonalizado`. A mensagem
sensível nunca é enviada à IA para classificação. Se o Telegram não permitir a
exclusão, a resposta orienta apagar a mensagem manualmente sem repetir o segredo.
`/cancel` abandona uma captura pendente.

O fluxo normal não exige que a pessoa usuária execute `/secret`. Quando o Codex
identifica uma credencial ausente, ele chama o broker local:

```powershell
python interfaces/telegram/scripts/request_credential.py `
  --entry "APIs/Omie" `
  --field "username:App Key" `
  --field "password:App Secret" `
  --prompt "Informe as credenciais para ativar a integração Omie."
```

O script recebe `chat_id` e caixa do trabalho somente pelo ambiente definido pelo
gateway. Ele nunca recebe o valor protegido: publica apenas a descrição da necessidade
e aguarda. O gateway pergunta cada campo diretamente no Telegram, intercepta e tenta
apagar cada resposta, mantém valores intermediários somente em memória, grava
`Username` e `Password` juntos e devolve ao processo apenas sucesso ou erro. Enquanto
a captura estiver ativa, o prazo do turno é estendido de forma limitada.

O caminho é escolhido pelo Codex conforme a skill e o perfil da integração. Assim,
contas diferentes podem usar entradas como `APIs/Omie/EmpresaA` e
`APIs/Omie/EmpresaB`, sem expor a estrutura do cofre à pessoa usuária. Uma captura
simples aceita `password`; a captura dupla aceita `username` e `password`. Valores
adicionais devem usar entradas protegidas separadas até existir contrato público para
atributos personalizados. O comando `/secret` permanece somente como fallback manual.

Responder a uma mensagem inclui um único nível de contexto, quote e anexos conhecidos,
sem trocar a thread ativa. A confirmação, a resposta final e o primeiro artefato usam
`reply_parameters` para apontar nativamente à solicitação. IDs devolvidos pelo Telegram,
thread e turn são persistidos no SQLite.

No backend `exec`, o gateway usa `codex exec --json` e `codex exec resume`. No backend
`app-server`, inicia um transporte stdio, faz `initialize`/`initialized`, cria ou retoma
a thread, usa `localImage` para imagens e encerra somente em `turn/completed`. O App
Server fornece deltas de `agentMessage` classificados como `commentary` ou
`final_answer`. Somente `commentary` pode alimentar o progresso detalhado; itens de
ferramentas são convertidos em descrições neutras. Eventos `reasoning`, argumentos,
comandos, caminhos, logs e saída bruta nunca são retransmitidos. O backend `exec`
oferece os mesmos marcos quando o evento JSONL correspondente estiver disponível, mas
pode não fornecer deltas de texto com a mesma granularidade.

O transporte principal usa `sendMessageDraft`, com um identificador estável por job,
para exibir uma prévia efêmera marcada explicitamente como não final. As atualizações
de texto são limitadas para evitar uma chamada por token. Se o método não estiver
disponível, o gateway cria uma única mensagem de progresso e passa a editá-la. Ao
concluir, o draft é removido e a resposta final continua sendo enviada separadamente.
`/cancel` termina o processo `exec` ou envia `turn/interrupt` ao App Server.

## Trabalhos e artefatos

Cada solicitação cria uma caixa isolada em `data/telegram/jobs/<job-id>/`:

```text
input/                 arquivos atuais e referenciados
derived/               conteúdo preparado pelos processadores
output/                únicos arquivos elegíveis para envio
delivery-schema.json   contrato fornecido ao Codex
result.json            resposta estruturada original
```

O processo do Codex recebe `COWORKER_JOB_OUTPUT` e `COWORKER_JOB_DERIVED` com os
caminhos absolutos da caixa atual, além de `COWORKER_JOB_INPUT` somente para validar
arquivos recebidos. Scripts fechados podem usar `derived/` para entradas intermediárias;
não devem aceitar um destino alternativo nem sobrescrever arquivos.

### Entradas intermediárias de skills

Toda skill que precise transformar campos tipados em um envelope JSON deve importar
`interfaces.telegram.job_context.write_job_json`. Esse é o único componente responsável
por interpretar `COWORKER_JOB_DERIVED`, confinar o destino em `data/`, gerar o nome pela
chave idempotente, serializar deterministicamente e criar sem sobrescrita. A skill
continua responsável por validar seu contrato de negócio antes de chamar o componente.

O ponto de entrada público da skill não deve receber caminho de destino ou JSON completo
na linha de comando. Ele devolve somente `path` e `created`; preparar o documento não
autoriza sua aplicação. Se uma integração precisar de outro formato, o suporte deve ser
adicionado ao módulo central com testes de confinamento e idempotência, em vez de criar
uma implementação paralela.

### Limitação conhecida: edição direta no gateway restrito

> [!IMPORTANT]
> Em workspaces Windows iniciados pelo backend Telegram, a ferramenta de patch pode
> rejeitar um caminho válido abaixo de `data/` como externo ao projeto. O executor
> também não oferece stdin nativo, e pipelines ou shells genéricos permanecem bloqueados
> por projeto. Não contornar isso ampliando `config/codex.rules`: use um ponto de entrada
> tipado da skill apoiado por `job_context.py`.

Um artefato declarado precisa ser relativo a `output/`, regular, não vazio, estar sob
o limite de upload e permanecer dentro da pasta após `Path.resolve(strict=True)`.
Links, junctions e reparse points que escapem da saída são rejeitados. O gateway
detecta MIME por assinatura quando possível, calcula SHA-256 e escolhe o método nativo:
JPEG/WebP e PNG sem alpha como foto; PNG com alpha e arquivos genéricos como documento;
GIF como animação; áudio convencional como áudio; OGG como voz; vídeo como vídeo.

O publicador `interfaces/telegram/scripts/publish_artifact.py` é o único comando de
cópia liberado ao Codex. Ele recebe o destino somente por `COWORKER_JOB_OUTPUT`, não
sobrescreve arquivos e devolve apenas metadados seguros. Arquivos em
`<CODEX_HOME>/generated_images` são concedidos somente para leitura.

## Processadores

Sem dependências externas, a interface prepara texto UTF-8, JSON, CSV, XML, inventário
de ZIP e extração básica de DOCX/XLSX por XML interno. ZIP possui limites de membros e
tamanho descompactado e rejeita path traversal. Imagens seguem como entrada visual
nativa. PDFs pesquisáveis são extraídos localmente com `pypdf`, respeitando os limites
de páginas e caracteres de `[processors]`; metadados devolvem somente nomes de campos,
nunca valores. O comando fechado abaixo permite repetir a leitura sem aceitar caminhos
fora do `input/` atual:

```powershell
python interfaces/telegram/scripts/extract_pdf_text.py --input CAMINHO_DO_PDF
```

O JSON informa `needs_ocr: true` quando não há texto pesquisável. OCR não é executado
nem enviado a serviço remoto automaticamente; o original é preservado. Se `pypdf` não
estiver instalado, o diagnóstico orienta `python -m pip install -r requirements.txt`.
Vídeo mantém apenas diagnóstico opcional. Áudio pode
ser transcrito pelo EccoVox configurado em `[processors.transcription]`, por CLI
isolado ou HTTP. O configurador local oferece host, porta, instalação e modelo. Uma
mensagem Telegram do tipo `voice` com confiança
suficiente é incorporada ao pedido atual como fala da pessoa usuária; arquivos do tipo
`audio` continuam sendo conteúdo anexado. Em baixa confiança, o Codex recebe somente
uma hipótese e deve pedir confirmação antes de executar ações. A interface nunca
instala processadores automaticamente nem executa arquivos recebidos.

O EccoVox aceita um prompt curto, termos contextuais e aliases confirmados no formato
`origem=destino`. Aliases são substituições explícitas, não correções fonéticas
inferidas. O gateway não inclui texto bruto descartado, stderr do motor ou caminhos de
bibliotecas no prompt. Um backend HTTP remoto exige HTTPS e `allow_remote = true`,
pois envia o áudio para outra máquina; o padrão permanece restrito a loopback. O
backend CLI executa sem shell e com argumentos separados. Com `backend = "http"` e `auto_start = true`, o
gateway inicia um servidor EccoVox oculto quando o endpoint ainda não estiver pronto,
mantém o modelo aquecido e encerra somente o processo que ele próprio iniciou.

Os limites ficam em `[processors]` e `[media]` no TOML privado. A seção opcional
`[feedback]` controla `typing_interval_seconds` e permite substituir as listas
`immediate_messages` e `queued_messages`; sem substituição, cada estado usa 30
variações internas. O gateway renova `typing` enquanto um trabalho estiver em execução.
A retenção dos jobs é
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

Por padrão, o SQLite operacional fica em
`%LOCALAPPDATA%\Coworker\instances\<instance_id>\telegram`. Isso evita
sincronização concorrente de um banco ativo. Entradas, derivados e saídas ficam em
`data/telegram/jobs/`, com metadados, tamanho e SHA-256, e permanecem ignorados pelo Git.

A autorização usa os IDs numéricos do usuário e da conversa, nunca o `@username`.
Mensagens de grupos, usuários não autorizados e tipos desconhecidos não chegam ao
Codex. Arquivos recebidos são tratados como conteúdo não confiável e nunca são
executados pelo gateway.

Use `workspace-write` como sandbox inicial. O gateway converte essa opção em um perfil
de permissões explícito do Codex: o projeto é somente leitura e apenas `data/` recebe
escrita. O campo
`codex.network_access` controla separadamente a rede dos comandos executados. Libere
outros diretórios para leitura em `codex.additional_directories` ou, deliberadamente,
para escrita em `codex.writable_directories`; não use
`danger-full-access` para compensar uma configuração incompleta.

Na configura&ccedil;&atilde;o interativa, as listas de leitura e escrita s&atilde;o
gerenciadas incrementalmente: novos caminhos podem ser informados separados por
v&iacute;rgula, e cada item pode ser removido individualmente. O separador antigo por
ponto e v&iacute;rgula continua aceito. Caminhos relativos s&atilde;o resolvidos a partir
da raiz do RodriClone; `.` representa o pr&oacute;prio reposit&oacute;rio.

Para escrita, o menu oferece dois atalhos seguros: manter somente `data/`, que
continua sendo o padr&atilde;o de novas inst&acirc;ncias, ou adicionar `.` para permitir
que uma inst&acirc;ncia altere seu pr&oacute;prio reposit&oacute;rio. Outros caminhos formam o
perfil personalizado. A permiss&atilde;o de arquivos n&atilde;o libera comandos por si s&oacute;:
os pontos de entrada necess&aacute;rios ainda devem existir em `config/codex.rules`.


Quando a pessoa propriet&aacute;ria realmente precisar de uma inst&acirc;ncia com acesso
global, use a op&ccedil;&atilde;o **10. Super inst&acirc;ncia** no configurador local. A
ativa&ccedil;&atilde;o exige duas confirma&ccedil;&otilde;es e grava
`codex.access_mode = "super"`. Esse perfil for&ccedil;a `danger-full-access`, rede
habilitada e `approval_policy = "never"`; tamb&eacute;m remove somente
`<codex.home_dir>/rules/gateway.rules`, que &eacute; o arquivo restritivo gerado pelo
gateway. Outras regras mantidas pela pessoa propriet&aacute;ria n&atilde;o s&atilde;o
apagadas.

O modo super executa com todas as permiss&otilde;es da conta do Windows que iniciou o
gateway. Ele pode ler, alterar ou excluir dados e executar Git, PowerShell, Python e
outros aplicativos dispon&iacute;veis. A ativa&ccedil;&atilde;o nunca pode ser feita por
um comando do Telegram. Reinicie o gateway ap&oacute;s ativar ou desativar o perfil.

Uma super inst&acirc;ncia gerenciada pode agendar o pr&oacute;prio rein&iacute;cio com:

`python interfaces/telegram/scripts/restart_gateway.py request`

O pedido n&atilde;o encerra diretamente o processo que o executou. O gateway cria um
relan&ccedil;ador externo destacado, para de buscar novas atualiza&ccedil;&otilde;es e
drena o trabalho atual e os itens que j&aacute; estavam na fila. Depois que o PID antigo
termina, o relan&ccedil;ador inicia outra c&oacute;pia, confirma o novo PID e sai.

O estado transit&oacute;rio usa `gateway-restart-request.json` e
`gateway-restart-worker.json` dentro de `state_dir`; o diagn&oacute;stico fica em
`gateway-restart.log`. O relan&ccedil;ador &eacute; criado pelo gateway, e n&atilde;o
pelo processo tempor&aacute;rio do Codex, para sobreviver ao encerramento da &aacute;rvore
do trabalho. Solicita&ccedil;&otilde;es duplicadas s&atilde;o recusadas.

Com `approval_policy = "never"`, um comando que não corresponda às regras públicas é
recusado em vez de solicitar aprovação pela conversa. Ao criar um novo script que a
interface deva executar, adicione seu ponto de entrada ao modelo de regras, sem liberar
o interpretador Python ou o PowerShell de forma genérica.

## Estado e recuperação

```powershell
python interfaces/telegram/gateway.py status
python interfaces/telegram/gateway.py pairing cancel
```

O `update_id` do Telegram é persistido antes do processamento para impedir execução
duplicada. Uma sessão só é substituída depois que o Codex devolve um novo
`thread_id`. Falhas não revelam token, senha mestra nem saída bruta do processo.

Ao reiniciar, jobs que estavam na fila ou executando são marcados como falhos. Um
artefato que estava em `uploading` passa para `unknown`: ele não é reenviado
automaticamente, pois a Bot API pode ter aceitado o upload antes da queda e não oferece
uma chave geral de idempotência para essa operação.
### Grupos, tópicos e scheduler

Grupos configurados como supergrupo-fórum passam por validação de privacidade
desligada, permissão de gerenciamento de tópicos e membros autorizados. Todas as
mensagens são registradas para formar contexto, mas somente menções, respostas ao
bot, comandos e aprovações chegam ao Codex.

O scheduler privado usa `state_dir/scheduler.sqlite3` e é iniciado/encerrado com o
gateway. O MVP executa somente scripts Python existentes em `data/`, `interfaces/`
ou `skills/`, sem shell, código inline ou caminho externo. A retenção de mensagens,
anexos, artefatos e resumos usa padrão mínimo de 180 dias, configurável para prazos
maiores no TOML privado.

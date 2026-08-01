# Migrations

Este diretório contém os schemas versionados dos bancos SQLite da BOTina. Os arquivos
da raiz pertencem à memória estruturada; `telegram/` pertence ao estado operacional da
interface Telegram.

Cada alteração deve ser adicionada em um novo arquivo SQL, usando numeração crescente.
Uma migration aplicada nunca deve ser alterada. O utilitário verifica o checksum dos
arquivos e interrompe a execução quando detecta divergência.

O schema Telegram inicial é `001_initial.sql`; capacidades estruturadas de mensagens,
jobs, threads, turnos e artefatos são acrescentadas por `002_structured_delivery.sql`,
preservando bancos já existentes.

Os bancos locais não são versionados. A memória fica em `data/memory.sqlite3`; o banco
do Telegram fica, por padrão, em `%LOCALAPPDATA%\BOTina\telegram\telegram.sqlite3`.

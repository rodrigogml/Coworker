# Migrations

Este diretório contém o schema versionado da memória SQLite.

Cada alteração deve ser adicionada em um novo arquivo SQL, usando numeração crescente.
Uma migration aplicada nunca deve ser alterada. O utilitário verifica o checksum dos
arquivos e interrompe a execução quando detecta divergência.

O banco local permanece em `data/memory.sqlite3` e não é versionado.

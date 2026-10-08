-- Execute no banco Bronze DBRO684.
-- A Landing nao recebe schema nem tabelas de controle da carga.
-- O contrato SQL de controle usa exclusivamente as quatro tabelas dbo abaixo.
-- Informe um UUID em @ExecutionId para filtrar uma execucao; NULL mostra todas.
-- Script exclusivamente de leitura.
USE [DBRO684];
GO

SET NOCOUNT ON;

DECLARE @ExecutionId uniqueidentifier = NULL;

IF OBJECT_ID(N'dbo.versao_esquema', N'U') IS NULL
   OR OBJECT_ID(N'dbo.execucao', N'U') IS NULL
   OR OBJECT_ID(N'dbo.execucao_tabela', N'U') IS NULL
   OR OBJECT_ID(N'dbo.execucao_lote', N'U') IS NULL
    THROW 51001, 'Uma ou mais tabelas persistentes de controle nao existem em DBRO684.dbo.', 1;

/* Panorama por execucao. */
SELECT
    e.execution_id,
    e.dataset_id,
    e.destination_area,
    e.state,
    COUNT_BIG(t.table_id) AS total_tables,
    COALESCE(SUM(t.imported_rows), 0) AS imported_rows,
    MIN(e.started_at) AS started_at,
    MAX(e.updated_at) AS updated_at
FROM [dbo].[execucao] AS e
LEFT JOIN [dbo].[execucao_tabela] AS t
    ON  t.execution_id = e.execution_id
    AND t.dataset_id = e.dataset_id
WHERE @ExecutionId IS NULL OR e.execution_id = @ExecutionId
GROUP BY
    e.execution_id,
    e.dataset_id,
    e.destination_area,
    e.state
ORDER BY started_at DESC;

/* Checkpoint e progresso por tabela. */
SELECT
    t.execution_id,
    t.dataset_id,
    t.destination_schema,
    t.destination_table,
    t.state,
    t.index_state,
    t.last_exported_block,
    t.last_imported_block,
    COALESCE(b.confirmed_blocks, 0) AS confirmed_blocks,
    COALESCE(b.manifest_rows, 0) AS manifest_rows,
    t.imported_rows,
    t.import_cursor_json,
    t.final_limit_json,
    CONVERT(bit, CASE
        WHEN t.import_cursor_json = t.final_limit_json THEN 1
        ELSE 0
    END) AS final_limit_reached,
    t.updated_at
FROM [dbo].[execucao_tabela] AS t
OUTER APPLY
(
    SELECT
        COUNT_BIG(*) AS confirmed_blocks,
        COALESCE(SUM(bl.manifest_rows), 0) AS manifest_rows
    FROM [dbo].[execucao_lote] AS bl
    WHERE bl.execution_id = t.execution_id
      AND bl.dataset_id = t.dataset_id
      AND bl.table_id = t.table_id
) AS b
WHERE @ExecutionId IS NULL OR t.execution_id = @ExecutionId
ORDER BY
    t.execution_id DESC,
    t.destination_schema,
    t.destination_table;

/* Ultimos blocos confirmados no mesmo commit da carga. */
SELECT TOP (100)
    bl.execution_id,
    bl.dataset_id,
    t.destination_schema,
    t.destination_table,
    bl.block_number,
    bl.lower_bound_json,
    bl.upper_bound_json,
    bl.manifest_rows,
    bl.imported_rows,
    bl.file_bytes,
    bl.data_file_name,
    bl.data_sha256,
    bl.format_file_name,
    bl.format_sha256,
    bl.import_started_at,
    bl.commit_recorded_at
FROM [dbo].[execucao_lote] AS bl
INNER JOIN [dbo].[execucao_tabela] AS t
    ON  t.execution_id = bl.execution_id
    AND t.dataset_id = bl.dataset_id
    AND t.table_id = bl.table_id
WHERE @ExecutionId IS NULL OR bl.execution_id = @ExecutionId
ORDER BY bl.commit_recorded_at DESC;

"""Gera o SQL baixável que comprova uma marca d'água explicitamente informada.

O gerador não procura combinações candidatas nos dados. Banco, esquema, tabela
e colunas chegam da tela e são materializados no arquivo, que valida somente a
combinação recebida. Isso preserva a fronteira de responsabilidade do motor:
quem opera informa a marca; o SQL produz a evidência necessária para aceitá-la.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable


WATERMARK_VALIDATION_GUIDANCE_PT_BR = """\
Como usar:
1. Preencha banco, esquema, tabela de origem e a marca d'água na tela.
2. Informe somente os nomes das colunas, separados por vírgula. A ordem é
   sempre crescente (ASC) e a posição das colunas é preservada.
3. Baixe o arquivo e execute-o, com uma credencial somente leitura, na mesma
   instância SQL Server que hospeda a origem.
4. Analise a única linha retornada, principalmente marca_dagua_aceitavel e
   decisao_sugerida. O script valida apenas as colunas informadas; ele não tenta
   descobrir outra combinação.

O arquivo não altera objetos nem dados. A comprovação de NULL e unicidade usa
COUNT_BIG/GROUP BY e pode varrer a tabela. Sob READ COMMITTED, essa leitura pode
adquirir S-locks e bloquear gravações concorrentes durante partes da varredura.
Execute em janela apropriada e avalie um índice compatível ou isolamento por
versionamento já habilitado pela administração do banco. LOCK_TIMEOUT limita
somente quanto o script espera por bloqueios; ele não limita o tempo dos locks
adquiridos pelo próprio script. NOLOCK não é usado porque leituras sujas
invalidariam a evidência.
"""


_IDENTIFIER_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _identifier(value: object, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} é obrigatório")
    if len(text.encode("utf-16-le")) // 2 > 128:
        raise ValueError(f"{label} excede o limite SQL Server de 128 caracteres")
    if _IDENTIFIER_CONTROL_CHARACTERS.search(text):
        raise ValueError(f"{label} contém caractere de controle")
    return text


def _unicode_literal(value: str) -> str:
    """Return one safely escaped T-SQL Unicode string literal."""

    return "N'" + value.replace("'", "''") + "'"


def normalize_watermark_columns(columns: Iterable[object]) -> tuple[str, ...]:
    """Validate an ordered, ascending-only list of explicit column names."""

    result: list[str] = []
    seen: set[str] = set()
    for position, value in enumerate(columns, start=1):
        name = _identifier(value, f"Coluna {position} da marca d'água")
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"Marca d'água repete a coluna {name}")
        seen.add(folded)
        result.append(name)
    if not result:
        raise ValueError("Informe ao menos uma coluna para validar a marca d'água")
    return tuple(result)


def watermark_validation_filename(
    source_database: object, source_schema: object, source_table: object
) -> str:
    """Build a portable suggested filename without exposing path separators."""

    parts = (
        _identifier(source_database, "Banco de dados de origem"),
        _identifier(source_schema, "Esquema de origem"),
        _identifier(source_table, "Tabela de origem"),
    )
    safe = [re.sub(r"[^0-9A-Za-z_.-]+", "_", item).strip("._") or "objeto" for item in parts]
    return "validar_marca_dagua_" + "_".join(safe) + ".sql"


def render_watermark_validation_sql(
    *,
    source_database: object,
    source_schema: object,
    source_table: object,
    columns: Iterable[object],
) -> str:
    """Render a standalone SQL Server 2022+ validation script.

    Every value is embedded as a safely escaped Unicode literal. Object names
    are still passed through ``QUOTENAME`` by SQL Server before dynamic use.
    No unresolved macro is written to the downloaded file.
    """

    database = _identifier(source_database, "Banco de dados de origem")
    schema = _identifier(source_schema, "Esquema de origem")
    table = _identifier(source_table, "Tabela de origem")
    normalized_columns = normalize_watermark_columns(columns)
    columns_json = json.dumps(
        list(normalized_columns), ensure_ascii=False, separators=(",", ":")
    )
    rendered = _SQL_TEMPLATE
    for marker, value in (
        ("__SOURCE_DATABASE_LITERAL__", _unicode_literal(database)),
        ("__SOURCE_SCHEMA_LITERAL__", _unicode_literal(schema)),
        ("__SOURCE_TABLE_LITERAL__", _unicode_literal(table)),
        ("__WATERMARK_COLUMNS_JSON_LITERAL__", _unicode_literal(columns_json)),
    ):
        rendered = rendered.replace(marker, value)
    if re.search(r"__[A-Z][A-Z0-9_]*__", rendered):
        raise AssertionError("O SQL de validação reteve uma macro interna")
    return rendered.rstrip() + "\n"


_SQL_TEMPLATE = r"""/* ============================================================================
   VALIDAÇÃO DE MARCA D'ÁGUA EXPLÍCITA - SQL SERVER 2022+

   Arquivo gerado pelo BulkFlow - SQL Server Data Export & Load.
   - Banco, esquema, tabela e colunas já estão materializados abaixo.
   - A ordem da marca d'água é sempre ASC, na sequência informada.
   - A rotina NÃO descobre colunas: comprova somente a lista recebida.
   - A rotina não altera dados, tabelas, índices nem configurações.
   - A prova de unicidade usa COUNT_BIG/GROUP BY e pode varrer a tabela.
   - Sob READ COMMITTED, a varredura pode adquirir S-locks e bloquear DML.
   - LOCK_TIMEOUT limita a espera, não a duração dos locks já adquiridos.
   - NOLOCK não é usado: leitura suja não constitui prova de unicidade.
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;
SET DEADLOCK_PRIORITY LOW;
SET LOCK_TIMEOUT 15000;

DECLARE @SourceDatabase       sysname       = __SOURCE_DATABASE_LITERAL__;
DECLARE @SourceSchema         sysname       = __SOURCE_SCHEMA_LITERAL__;
DECLARE @SourceTable          sysname       = __SOURCE_TABLE_LITERAL__;
DECLARE @WatermarkColumnsJson nvarchar(max) = __WATERMARK_COLUMNS_JSON_LITERAL__;

IF DB_ID(@SourceDatabase) IS NULL
    THROW 51001, N'O banco de dados de origem informado não existe.', 1;

IF ISJSON(@WatermarkColumnsJson) <> 1
   OR NOT EXISTS
      (
          SELECT 1
          FROM OPENJSON(N'[' + @WatermarkColumnsJson + N']')
          WHERE [key] = N'0' AND [type] = 4
      )
    THROW 51002, N'A marca d''água deve ser um array JSON.', 1;

DECLARE @Sql nvarchar(max) = N'
USE ' + QUOTENAME(@SourceDatabase) + N';
SET NOCOUNT ON;

-- Torna o arquivo reexecutável na mesma sessão do SSMS/sqlcmd. As tabelas
-- temporárias são criadas somente depois do USE e adotam a collation da origem.
DROP TABLE IF EXISTS #candidate_key;
DROP TABLE IF EXISTS #validation_result;
DROP TABLE IF EXISTS #configured_column;

CREATE TABLE #configured_column
(
    ordinal_number int NOT NULL PRIMARY KEY,
    column_name    nvarchar(4000) COLLATE DATABASE_DEFAULT NULL,
    json_type      int NOT NULL
);

INSERT #configured_column (ordinal_number, column_name, json_type)
SELECT CONVERT(int, [key]) + 1, CONVERT(nvarchar(4000), [value]), [type]
FROM OPENJSON(@pColumnsJson);

IF NOT EXISTS (SELECT 1 FROM #configured_column)
    THROW 51003, N''Informe ao menos uma coluna para validar a marca d''''água.'', 1;

IF EXISTS
   (
       SELECT 1
       FROM #configured_column
       WHERE json_type <> 1
          OR column_name IS NULL
          OR LEN(LTRIM(RTRIM(column_name))) = 0
          OR DATALENGTH(column_name) > 256
   )
    THROW 51004, N''Cada coluna deve ser uma string não vazia que caiba em sysname.'', 1;

CREATE TABLE #validation_result
(
    banco_origem                         sysname COLLATE DATABASE_DEFAULT NOT NULL,
    esquema_origem                       sysname COLLATE DATABASE_DEFAULT NOT NULL,
    tabela_origem                        sysname COLLATE DATABASE_DEFAULT NOT NULL,
    tabela_existe                        bit            NOT NULL,
    colunas_configuradas                 nvarchar(max) COLLATE DATABASE_DEFAULT NULL,
    colunas_inexistentes                 nvarchar(max) COLLATE DATABASE_DEFAULT NULL,
    colunas_repetidas                    nvarchar(max) COLLATE DATABASE_DEFAULT NULL,
    colunas_com_tipo_nao_elegivel        nvarchar(max) COLLATE DATABASE_DEFAULT NULL,
    possui_pk_elegivel                   bit            NOT NULL,
    pk_elegivel                          nvarchar(max) COLLATE DATABASE_DEFAULT NULL,
    possui_unique_elegivel               bit            NOT NULL,
    unique_elegivel                      nvarchar(max) COLLATE DATABASE_DEFAULT NULL,
    total_registros                      bigint         NULL,
    registros_com_nulo_na_marca          bigint         NULL,
    grupos_com_duplicidade               bigint         NULL,
    registros_duplicados_excedentes      bigint         NULL,
    marca_dagua_distingue_registros      nvarchar(30) COLLATE DATABASE_DEFAULT NOT NULL,
    marca_dagua_aceitavel                bit            NOT NULL,
    decisao_sugerida                     nvarchar(2000) COLLATE DATABASE_DEFAULT NOT NULL
);

DECLARE @ObjectId                    int;
DECLARE @QualifiedTable              nvarchar(517);
DECLARE @ConfiguredColumns           nvarchar(max);
DECLARE @MissingColumns              nvarchar(max);
DECLARE @RepeatedColumns             nvarchar(max);
DECLARE @IneligibleTypeColumns       nvarchar(max);
DECLARE @GroupingList                nvarchar(max);
DECLARE @NullPredicate               nvarchar(max);
DECLARE @EvidenceSql                 nvarchar(max);
DECLARE @TotalRows                   bigint = NULL;
DECLARE @RowsWithNull                bigint = NULL;
DECLARE @DuplicateGroups             bigint = NULL;
DECLARE @ExcessDuplicateRows         bigint = NULL;
DECLARE @HasEligiblePk               bit = 0;
DECLARE @HasEligibleUnique           bit = 0;
DECLARE @EligiblePk                  nvarchar(max);
DECLARE @EligibleUnique              nvarchar(max);

SET @QualifiedTable = QUOTENAME(@pSchema) + N''.'' + QUOTENAME(@pTable);
SET @ObjectId = OBJECT_ID(@QualifiedTable, N''U'');

SELECT @ConfiguredColumns = STRING_AGG(
           CONVERT(nvarchar(max), N''['' + REPLACE(column_name, N'']'', N'']]'') + N'']''),
           N'', ''
       ) WITHIN GROUP (ORDER BY ordinal_number)
FROM #configured_column;

SELECT @RepeatedColumns = STRING_AGG(
           CONVERT(nvarchar(max), N''['' + REPLACE(column_name, N'']'', N'']]'') + N'']''),
           N'', ''
       )
FROM
(
    SELECT column_name
    FROM #configured_column
    GROUP BY column_name
    HAVING COUNT_BIG(*) > 1
) AS repeated;

IF @ObjectId IS NULL
BEGIN
    INSERT #validation_result
    (
        banco_origem, esquema_origem, tabela_origem, tabela_existe,
        colunas_configuradas, colunas_inexistentes, colunas_repetidas,
        colunas_com_tipo_nao_elegivel, possui_pk_elegivel, pk_elegivel,
        possui_unique_elegivel, unique_elegivel, total_registros,
        registros_com_nulo_na_marca, grupos_com_duplicidade,
        registros_duplicados_excedentes, marca_dagua_distingue_registros,
        marca_dagua_aceitavel, decisao_sugerida
    )
    VALUES
    (
        @pDatabase, @pSchema, @pTable, 0, @ConfiguredColumns, NULL,
        @RepeatedColumns, NULL, 0, NULL, 0, NULL, NULL, NULL, NULL, NULL,
        N''NÃO AVALIADO'', 0, N''ERRO: a tabela de origem informada não existe.''
    );
    GOTO ReturnResult;
END;

SELECT @MissingColumns = STRING_AGG(
           CONVERT(nvarchar(max), N''['' + REPLACE(cfg.column_name, N'']'', N'']]'') + N'']''),
           N'', ''
       )
FROM #configured_column AS cfg
LEFT JOIN sys.columns AS c
  ON c.object_id = @ObjectId AND c.name = cfg.column_name
WHERE c.column_id IS NULL;

SELECT @IneligibleTypeColumns = STRING_AGG(
           CONVERT(nvarchar(max),
               N''['' + REPLACE(cfg.column_name, N'']'', N'']]'') + N''] (''
               + COALESCE(st.name, ut.name) + N'')''),
           N'', ''
       )
FROM #configured_column AS cfg
JOIN sys.columns AS c
  ON c.object_id = @ObjectId AND c.name = cfg.column_name
LEFT JOIN sys.types AS st
  ON st.system_type_id = c.system_type_id
 AND st.user_type_id = st.system_type_id
JOIN sys.types AS ut ON ut.user_type_id = c.user_type_id
WHERE COALESCE(st.name, ut.name) NOT IN
      (N''bigint'', N''int'', N''smallint'', N''tinyint'', N''bit'',
       N''decimal'', N''numeric'', N''money'', N''smallmoney'', N''date'',
       N''datetime'', N''smalldatetime'', N''datetime2'', N''datetimeoffset'',
       N''time'', N''char'', N''varchar'', N''nchar'', N''nvarchar'',
       N''binary'', N''varbinary'', N''uniqueidentifier'')
   OR (COALESCE(st.name, ut.name) IN (N''varchar'', N''nvarchar'', N''varbinary'')
       AND c.max_length = -1);

CREATE TABLE #candidate_key
(
    index_id           int           NOT NULL PRIMARY KEY,
    index_name         sysname COLLATE DATABASE_DEFAULT NOT NULL,
    key_kind           varchar(6) COLLATE DATABASE_DEFAULT NOT NULL,
    key_columns        nvarchar(max) COLLATE DATABASE_DEFAULT NOT NULL,
    has_nullable       bit           NOT NULL,
    has_nulls_in_data  bit           NOT NULL DEFAULT (0)
);

INSERT #candidate_key (index_id, index_name, key_kind, key_columns, has_nullable)
SELECT
    i.index_id,
    i.name,
    CASE WHEN i.is_primary_key = 1 THEN ''PK'' ELSE ''UNIQUE'' END,
    STRING_AGG(CONVERT(nvarchar(max), QUOTENAME(c.name)), N'', '')
        WITHIN GROUP (ORDER BY ic.key_ordinal),
    CONVERT(bit, MAX(CONVERT(int, c.is_nullable)))
FROM sys.indexes AS i
JOIN sys.index_columns AS ic
  ON ic.object_id = i.object_id
 AND ic.index_id = i.index_id
 AND ic.key_ordinal > 0
JOIN sys.columns AS c
  ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = @ObjectId
  AND i.type IN (1, 2)
  AND i.is_unique = 1
  AND i.has_filter = 0
  AND i.is_disabled = 0
  AND i.is_hypothetical = 0
  AND NOT EXISTS
      (
          SELECT 1
          FROM sys.index_columns AS bad_ic
          JOIN sys.columns AS bad_c
            ON bad_c.object_id = bad_ic.object_id
           AND bad_c.column_id = bad_ic.column_id
          LEFT JOIN sys.types AS bad_st
            ON bad_st.system_type_id = bad_c.system_type_id
           AND bad_st.user_type_id = bad_st.system_type_id
          JOIN sys.types AS bad_ut ON bad_ut.user_type_id = bad_c.user_type_id
          WHERE bad_ic.object_id = i.object_id
            AND bad_ic.index_id = i.index_id
            AND bad_ic.key_ordinal > 0
            AND
                (
                    COALESCE(bad_st.name, bad_ut.name) NOT IN
                    (N''bigint'', N''int'', N''smallint'', N''tinyint'', N''bit'',
                     N''decimal'', N''numeric'', N''money'', N''smallmoney'', N''date'',
                     N''datetime'', N''smalldatetime'', N''datetime2'', N''datetimeoffset'',
                     N''time'', N''char'', N''varchar'', N''nchar'', N''nvarchar'',
                     N''binary'', N''varbinary'', N''uniqueidentifier'')
                    OR
                    (COALESCE(bad_st.name, bad_ut.name) IN
                     (N''varchar'', N''nvarchar'', N''varbinary'')
                     AND bad_c.max_length = -1)
                )
      )
GROUP BY i.index_id, i.name, i.is_primary_key;

DECLARE @CandidateIndexId int;
DECLARE @CandidateNullPredicate nvarchar(max);
DECLARE @CandidateHasNull bit;
DECLARE @CandidateProbeSql nvarchar(max);

DECLARE candidate_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT index_id FROM #candidate_key WHERE has_nullable = 1 ORDER BY index_id;
OPEN candidate_cursor;
FETCH NEXT FROM candidate_cursor INTO @CandidateIndexId;
WHILE @@FETCH_STATUS = 0
BEGIN
    SELECT @CandidateNullPredicate = STRING_AGG(
               CONVERT(nvarchar(max), QUOTENAME(c.name) + N'' IS NULL''), N'' OR ''
           ) WITHIN GROUP (ORDER BY ic.key_ordinal)
    FROM sys.index_columns AS ic
    JOIN sys.columns AS c
      ON c.object_id = ic.object_id AND c.column_id = ic.column_id
    WHERE ic.object_id = @ObjectId
      AND ic.index_id = @CandidateIndexId
      AND ic.key_ordinal > 0
      AND c.is_nullable = 1;

    SET @CandidateProbeSql = N''SELECT @HasNullOut = CONVERT(bit,
        CASE WHEN EXISTS (SELECT 1 FROM '' + @QualifiedTable + N'' WHERE ''
        + @CandidateNullPredicate + N'') THEN 1 ELSE 0 END);'';
    SET @CandidateHasNull = 0;
    EXEC sys.sp_executesql @CandidateProbeSql,
         N''@HasNullOut bit OUTPUT'', @HasNullOut = @CandidateHasNull OUTPUT;
    UPDATE #candidate_key
       SET has_nulls_in_data = @CandidateHasNull
     WHERE index_id = @CandidateIndexId;

    FETCH NEXT FROM candidate_cursor INTO @CandidateIndexId;
END;
CLOSE candidate_cursor;
DEALLOCATE candidate_cursor;

SELECT
    @HasEligiblePk = CONVERT(bit, CASE WHEN EXISTS
        (SELECT 1 FROM #candidate_key WHERE key_kind = ''PK'' AND has_nulls_in_data = 0)
        THEN 1 ELSE 0 END),
    @HasEligibleUnique = CONVERT(bit, CASE WHEN EXISTS
        (SELECT 1 FROM #candidate_key WHERE key_kind = ''UNIQUE'' AND has_nulls_in_data = 0)
        THEN 1 ELSE 0 END);

SELECT @EligiblePk = STRING_AGG(
           CONVERT(nvarchar(max), QUOTENAME(index_name) + N'': '' + key_columns), N''; ''
       )
FROM #candidate_key
WHERE key_kind = ''PK'' AND has_nulls_in_data = 0;

SELECT @EligibleUnique = STRING_AGG(
           CONVERT(nvarchar(max), QUOTENAME(index_name) + N'': '' + key_columns), N''; ''
       )
FROM #candidate_key
WHERE key_kind = ''UNIQUE'' AND has_nulls_in_data = 0;

IF @MissingColumns IS NULL
   AND @RepeatedColumns IS NULL
   AND @IneligibleTypeColumns IS NULL
BEGIN
    SELECT
        @GroupingList = STRING_AGG(CONVERT(nvarchar(max), QUOTENAME(c.name)), N'', '')
            WITHIN GROUP (ORDER BY cfg.ordinal_number),
        @NullPredicate = STRING_AGG(
            CONVERT(nvarchar(max), QUOTENAME(c.name) + N'' IS NULL''), N'' OR ''
        ) WITHIN GROUP (ORDER BY cfg.ordinal_number)
    FROM #configured_column AS cfg
    JOIN sys.columns AS c
      ON c.object_id = @ObjectId AND c.name = cfg.column_name;

    SET @EvidenceSql = N''
        SELECT
            @TotalRowsOut = COUNT_BIG(*),
            @RowsWithNullOut = COALESCE(SUM(CONVERT(bigint,
                CASE WHEN '' + @NullPredicate + N'' THEN 1 ELSE 0 END)), 0)
        FROM '' + @QualifiedTable + N'';

        SELECT
            @DuplicateGroupsOut = COUNT_BIG(*),
            @ExcessDuplicateRowsOut = COALESCE(SUM(group_rows - 1), 0)
        FROM
        (
            SELECT COUNT_BIG(*) AS group_rows
            FROM '' + @QualifiedTable + N''
            GROUP BY '' + @GroupingList + N''
            HAVING COUNT_BIG(*) > 1
        ) AS duplicate_group;'';

    EXEC sys.sp_executesql @EvidenceSql,
         N''@TotalRowsOut bigint OUTPUT, @RowsWithNullOut bigint OUTPUT,
            @DuplicateGroupsOut bigint OUTPUT, @ExcessDuplicateRowsOut bigint OUTPUT'',
         @TotalRowsOut = @TotalRows OUTPUT,
         @RowsWithNullOut = @RowsWithNull OUTPUT,
         @DuplicateGroupsOut = @DuplicateGroups OUTPUT,
         @ExcessDuplicateRowsOut = @ExcessDuplicateRows OUTPUT;
END;

DECLARE @HasStructuralKey bit = CONVERT(bit,
    CASE WHEN @HasEligiblePk = 1 OR @HasEligibleUnique = 1 THEN 1 ELSE 0 END);
DECLARE @WatermarkAcceptable bit = CONVERT(bit,
    CASE
        WHEN @MissingColumns IS NOT NULL OR @RepeatedColumns IS NOT NULL
          OR @IneligibleTypeColumns IS NOT NULL OR @RowsWithNull IS NULL THEN 0
        WHEN @RowsWithNull > 0 THEN 0
        WHEN @HasStructuralKey = 1 THEN 1
        WHEN @TotalRows = 0 THEN 0
        WHEN @DuplicateGroups = 0 THEN 1
        ELSE 0
    END);

INSERT #validation_result
(
    banco_origem, esquema_origem, tabela_origem, tabela_existe,
    colunas_configuradas, colunas_inexistentes, colunas_repetidas,
    colunas_com_tipo_nao_elegivel, possui_pk_elegivel, pk_elegivel,
    possui_unique_elegivel, unique_elegivel, total_registros,
    registros_com_nulo_na_marca, grupos_com_duplicidade,
    registros_duplicados_excedentes, marca_dagua_distingue_registros,
    marca_dagua_aceitavel, decisao_sugerida
)
SELECT
    @pDatabase, @pSchema, @pTable, 1, @ConfiguredColumns, @MissingColumns,
    @RepeatedColumns, @IneligibleTypeColumns, @HasEligiblePk, @EligiblePk,
    @HasEligibleUnique, @EligibleUnique, @TotalRows, @RowsWithNull,
    @DuplicateGroups, @ExcessDuplicateRows,
    CASE
        WHEN @TotalRows IS NULL THEN N''NÃO AVALIADO''
        WHEN @TotalRows = 0 THEN N''INCONCLUSIVO: VAZIA''
        WHEN @DuplicateGroups = 0 THEN N''SIM''
        ELSE N''NÃO''
    END,
    @WatermarkAcceptable,
    CASE
        WHEN @RepeatedColumns IS NOT NULL THEN
            N''CORRIGIR: a marca d''''água repete uma ou mais colunas.''
        WHEN @MissingColumns IS NOT NULL THEN
            N''CORRIGIR: uma ou mais colunas informadas não existem na tabela.''
        WHEN @IneligibleTypeColumns IS NOT NULL THEN
            N''CORRIGIR: uma ou mais colunas possuem tipo não comparável pelo motor.''
        WHEN @RowsWithNull > 0 THEN
            N''REJEITAR: a marca d''''água contém valores NULL.''
        WHEN @HasStructuralKey = 1 THEN
            N''ACEITAR: existe PK/UNIQUE elegível sem NULL e a marca explícita é comparável, ascendente e sem NULL. Duplicidades da marca são exportadas como grupo completo.''
        WHEN @TotalRows = 0 THEN
            N''INCONCLUSIVO: sem PK/UNIQUE elegível, uma tabela vazia não comprova a marca explícita.''
        WHEN @DuplicateGroups > 0 THEN
            N''REJEITAR: sem PK/UNIQUE elegível, a combinação explícita precisa distinguir unicamente os registros atuais.''
        WHEN @TotalRows > 0 AND @DuplicateGroups = 0 THEN
            N''ACEITAR: sem PK/UNIQUE elegível, a combinação explícita foi comprovada nos dados atuais, sem NULL e sem duplicidade.''
        ELSE
            N''NÃO AVALIADO: não foi possível obter evidência conclusiva.''
    END;

ReturnResult:
SELECT
    banco_origem,
    esquema_origem,
    tabela_origem,
    tabela_existe,
    colunas_configuradas,
    colunas_inexistentes,
    colunas_repetidas,
    colunas_com_tipo_nao_elegivel,
    possui_pk_elegivel,
    pk_elegivel,
    possui_unique_elegivel,
    unique_elegivel,
    total_registros,
    registros_com_nulo_na_marca,
    grupos_com_duplicidade,
    registros_duplicados_excedentes,
    marca_dagua_distingue_registros,
    marca_dagua_aceitavel,
    decisao_sugerida
FROM #validation_result;

DROP TABLE IF EXISTS #candidate_key;
DROP TABLE IF EXISTS #validation_result;
DROP TABLE IF EXISTS #configured_column;
';

EXEC sys.sp_executesql
    @Sql,
    N'@pDatabase sysname, @pSchema sysname, @pTable sysname,
      @pColumnsJson nvarchar(max)',
    @pDatabase = @SourceDatabase,
    @pSchema = @SourceSchema,
    @pTable = @SourceTable,
    @pColumnsJson = @WatermarkColumnsJson;
"""


__all__ = [
    "WATERMARK_VALIDATION_GUIDANCE_PT_BR",
    "normalize_watermark_columns",
    "render_watermark_validation_sql",
    "watermark_validation_filename",
]

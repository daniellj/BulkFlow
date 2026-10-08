/*
Migra o controle persistente do BulkFlow no DBRO684 da versao fisica 2 para 3.
Execute como administrador, sem cargas BulkFlow ativas. A transacao preserva
todo o historico; nenhuma tabela ou linha e removida.
*/
SET NOCOUNT ON;
SET XACT_ABORT ON;
USE [DBRO684];

BEGIN TRY
    BEGIN TRANSACTION;

    DECLARE @old_count int =
          IIF(OBJECT_ID(N'dbo.versao_esquema', N'U') IS NULL, 0, 1)
        + IIF(OBJECT_ID(N'dbo.execucao', N'U') IS NULL, 0, 1)
        + IIF(OBJECT_ID(N'dbo.execucao_tabela', N'U') IS NULL, 0, 1)
        + IIF(OBJECT_ID(N'dbo.execucao_lote', N'U') IS NULL, 0, 1);
    DECLARE @new_count int =
          IIF(OBJECT_ID(N'dbo.ctl_exec_versao', N'U') IS NULL, 0, 1)
        + IIF(OBJECT_ID(N'dbo.ctl_exec', N'U') IS NULL, 0, 1)
        + IIF(OBJECT_ID(N'dbo.ctl_exec_tabela', N'U') IS NULL, 0, 1)
        + IIF(OBJECT_ID(N'dbo.ctl_exec_lote', N'U') IS NULL, 0, 1);

    IF @old_count = 0 AND @new_count = 4
    BEGIN
        IF NOT EXISTS
           (SELECT 1 FROM [dbo].[ctl_exec_versao] WHERE [version] = 3)
            THROW 52700, 'Controle ctl_exec existente nao esta na versao 3.', 1;
        COMMIT TRANSACTION;
        PRINT 'Controle BulkFlow ja estava na versao 3.';
        RETURN;
    END;

    IF @old_count <> 4 OR @new_count <> 0
        THROW 52701, 'Controle v2 parcial ou objetos v3 concorrentes; migracao cancelada.', 1;
    IF (SELECT COUNT_BIG(*) FROM [dbo].[versao_esquema]) <> 1
       OR NOT EXISTS (SELECT 1 FROM [dbo].[versao_esquema] WHERE [version] = 2)
        THROW 52702, 'dbo.versao_esquema nao contem exatamente a versao 2.', 1;

    EXEC sys.sp_rename N'dbo.versao_esquema', N'ctl_exec_versao', N'OBJECT';
    EXEC sys.sp_rename N'dbo.execucao', N'ctl_exec', N'OBJECT';
    EXEC sys.sp_rename N'dbo.execucao_tabela', N'ctl_exec_tabela', N'OBJECT';
    EXEC sys.sp_rename N'dbo.execucao_lote', N'ctl_exec_lote', N'OBJECT';

    EXEC sys.sp_rename N'dbo.ctl_exec_versao.pk_versao_esquema', N'pk_ctl_exec_versao', N'INDEX';
    EXEC sys.sp_rename N'dbo.ctl_exec.pk_execucao', N'pk_ctl_exec', N'INDEX';
    EXEC sys.sp_rename N'dbo.ctl_exec_tabela.pk_execucao_tabela', N'pk_ctl_exec_tabela', N'INDEX';
    EXEC sys.sp_rename N'dbo.ctl_exec_tabela.uq_execucao_tabela_destino', N'uq_ctl_exec_tabela_destino', N'INDEX';
    EXEC sys.sp_rename N'dbo.ctl_exec_lote.pk_execucao_lote', N'pk_ctl_exec_lote', N'INDEX';
    EXEC sys.sp_rename N'dbo.ctl_exec_lote.uq_execucao_lote_numero', N'uq_ctl_exec_lote_numero', N'INDEX';

    EXEC sys.sp_rename N'dbo.df_versao_esquema_criado_em', N'df_ctl_exec_versao_criado_em', N'OBJECT';
    EXEC sys.sp_rename N'dbo.df_execucao_tabela_ultimo_lote_exportado', N'df_ctl_exec_tabela_ultimo_lote_exportado', N'OBJECT';
    EXEC sys.sp_rename N'dbo.df_execucao_tabela_ultimo_lote_importado', N'df_ctl_exec_tabela_ultimo_lote_importado', N'OBJECT';
    EXEC sys.sp_rename N'dbo.df_execucao_tabela_linhas_importadas', N'df_ctl_exec_tabela_linhas_importadas', N'OBJECT';
    EXEC sys.sp_rename N'dbo.fk_execucao_tabela_execucao', N'fk_ctl_exec_tabela_exec', N'OBJECT';
    EXEC sys.sp_rename N'dbo.fk_execucao_lote_execucao_tabela', N'fk_ctl_exec_lote_exec_tabela', N'OBJECT';

    UPDATE [dbo].[ctl_exec_versao] SET [version] = 3 WHERE [version] = 2;
    IF @@ROWCOUNT <> 1
        THROW 52703, 'A versao fisica do controle nao foi atualizada.', 1;

    COMMIT TRANSACTION;
    PRINT 'Controle BulkFlow migrado da versao 2 para 3 com historico preservado.';
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;

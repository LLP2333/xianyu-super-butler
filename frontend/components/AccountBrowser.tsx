import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, ExternalLink, Info, KeyRound, Loader2, MonitorSmartphone, Play, PlugZap, RefreshCw,
  ShieldCheck, Square, Trash2, X } from 'lucide-react';
import { accountBrowserAction, accountBrowserStatus, BrowserDesktop, BrowserStatus, setupBrowserAccount,
  browserSetupStatus, browserDesktopLink, clearBrowserData, reconnectBrowserListener, stopAccountBrowser, fetchBrowserCredentials } from '../services/api';
import { confirmAction } from '../services/feedback';
import { AccountDetail } from '../types';

const errorMessage = (error: unknown) => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' ? detail : error instanceof Error ? error.message : '操作失败';
};

type Props = { account?: AccountDetail; initialWindow?: Window | null; onClose: () => void; onChanged: () => void };

export default function AccountBrowser({ account, initialWindow, onClose, onChanged }: Props) {
  const [desktop, setDesktop] = useState<BrowserDesktop | null>(null);
  const [message, setMessage] = useState('正在准备原生浏览器…');
  const [status, setStatus] = useState<BrowserStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [reconnecting, setReconnecting] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [createdId, setCreatedId] = useState<string>();
  const accountId = account?.id || createdId;
  const windowRef = useRef(initialWindow);
  const previousState = useRef<string>();

  useEffect(() => {
    let cancelled = false;
    const request = account ? accountBrowserStatus(account.id) : setupBrowserAccount();
    void request.then(result => {
      if (cancelled) return;
      setDesktop(result); setMessage(result.message);
      if ('credentials_state' in result) {
        setStatus(result); windowRef.current?.close(); windowRef.current = null;
      }
      if ('open_url' in result && result.open_url && windowRef.current && !windowRef.current.closed) {
        windowRef.current.location.href = result.open_url;
        windowRef.current = null;
      }
      onChanged();
    }).catch(error => {
      if (!cancelled) { setMessage(errorMessage(error)); setStatusError(errorMessage(error)); }
      windowRef.current?.close(); windowRef.current = null;
    }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
    // List refreshes must not reopen the browser or restart the account lifecycle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account?.id]);

  useEffect(() => {
    if (busy) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = accountId ? await accountBrowserStatus(accountId) : await browserSetupStatus();
        if (cancelled) return;
        setStatus(result); setStatusError(null);
        if (result.account_id) setCreatedId(result.account_id);
        const current = `${result.account_id || accountId}:${result.credentials_state}`;
        if (current !== previousState.current) { previousState.current = current; onChanged(); }
      } catch (error) {
        if (!cancelled) setStatusError(errorMessage(error));
      }
      if (!cancelled) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, busy]);

  const controlBrowser = async (start: boolean) => {
    setBusy(true); setStatusError(null);
    try {
      const result = start
        ? accountId ? await accountBrowserAction(accountId, 'open') : await setupBrowserAccount()
        : await stopAccountBrowser();
      setDesktop(result); setMessage(result.message); setStatus(null); onChanged();
    } catch (error) { setStatusError(errorMessage(error)); }
    finally { setBusy(false); }
  };

  const fetchCredentials = async () => {
    if (!accountId) return;
    setBusy(true); setFetching(true); setStatusError(null);
    setMessage('正在打开闲鱼首页并点击消息…');
    try {
      const result = await fetchBrowserCredentials(accountId);
      setDesktop(result); setStatus(result); setMessage(result.message); onChanged();
    } catch (error) { setStatusError(errorMessage(error)); }
    finally { setBusy(false); setFetching(false); }
  };

  const reconnect = async () => {
    setBusy(true); setReconnecting(true); setStatusError(null);
    try {
      const result = desktop ? await reconnectBrowserListener()
        : accountId ? await accountBrowserAction(accountId, 'open') : await setupBrowserAccount();
      setDesktop(result); setMessage(result.message); setStatus(null);
    } catch (error) { setStatusError(errorMessage(error)); }
    finally { setBusy(false); setReconnecting(false); }
  };

  const openDesktop = async () => {
    const opened = window.open('about:blank', '_blank');
    if (opened) opened.opener = null;
    try {
      const { open_url } = await browserDesktopLink();
      if (opened) opened.location.href = open_url;
      else setMessage('浏览器阻止了新窗口，请允许此网站打开新窗口后重试。');
    } catch (error) { opened?.close(); setMessage(errorMessage(error)); }
  };

  const act = async (action: 'restart' | 'clear') => {
    if (action === 'clear' && !await confirmAction('清空浏览器的登录记录、Cookie、缓存及本地数据？账号设置保留，自动任务将暂停，需要重新登录。')) return;
    setBusy(true); setStatusError(null);
    try {
      const result = action === 'clear' ? await clearBrowserData() : await accountBrowserAction(accountId!, action);
      setDesktop(result); setMessage(result.message); setStatus(null); onChanged();
    } catch (error) { setStatusError(errorMessage(error)); }
    finally { setBusy(false); }
  };

  const running = status?.browser_running === true;
  const listening = !busy && !statusError && status?.listener_state === 'listening';
  const active = !busy && !statusError && status?.credentials_state === 'active';
  const titles = { waiting: '等待网页登录并捕获凭据', captured: '已捕获 Cookie 和消息 Token，正在自动采用',
    connecting: '凭据已保存，正在连接并验证消息服务',
    active: '消息认证成功，管家已连接', error: '凭据采用失败，账号保持暂停' };

  const browserTone: Tone = busy ? 'pending' : statusError ? 'error' : running ? 'ok' : 'idle';
  const listenerTone: Tone = busy ? 'pending' : statusError ? 'error' : !running ? 'idle'
    : listening ? 'ok' : status?.listener_state === 'connecting' ? 'pending' : 'warn';
  const credentialTone: Tone = statusError || status?.credentials_state === 'error' ? 'error'
    : active ? 'ok' : busy || !status || status.credentials_state !== 'waiting' ? 'pending' : 'warn';
  const CredentialIcon = credentialTone === 'ok' ? ShieldCheck : credentialTone === 'error' ? AlertTriangle
    : credentialTone === 'pending' ? Loader2 : KeyRound;

  return createPortal(
    <div className="modal-overlay">
      <div className="modal-container" style={{ maxWidth: 640, width: '96vw' }}>
        <div className="modal-header flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-[var(--radius)] bg-[var(--brand-soft)] text-[var(--brand-ink)]">
              <MonitorSmartphone size={20} />
            </span>
            <div className="min-w-0">
              <h3 className="text-base font-bold leading-tight">{account ? '原生浏览器' : '浏览器添加账号'}</h3>
              <p className="truncate text-xs text-[var(--text-muted)]">{account ? account.nickname || account.id : '在原生 Chrome 中登录新账号'}</p>
            </div>
          </div>
          <button type="button" aria-label="关闭窗口" title="关闭窗口" onClick={onClose}
            className="grid h-9 w-9 place-items-center rounded-full text-[var(--text-muted)] transition hover:bg-[var(--surface-strong)] hover:text-[var(--text)]">
            <X size={18} />
          </button>
        </div>

        <div className="space-y-4 overflow-y-auto p-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <StatusTile tone={browserTone} label="浏览器"
              value={busy ? '处理中' : running ? '运行中' : '已停止'}
              detail={busy ? message : running ? 'Chrome 正在运行' : '登录数据保留'} />
            <StatusTile tone={listenerTone} label="消息监听"
              value={busy ? reconnecting ? '重新连接中' : '连接中' : statusError ? '查询失败' : !running ? '未运行'
                : listening ? '监听中' : status?.listener_state === 'connecting' ? '连接中' : '已断开'}
              detail={statusError || status?.listener_error || (busy ? '请稍候…' : !running ? '已采用的 Token 仍由管家使用'
                : listening ? '正在接收网页登录结果' : '可点击“重新连接监听”恢复')} />
          </div>

          <div role="status" aria-live="polite" aria-atomic="true" className="flex gap-3 rounded-[var(--radius-lg)] border p-4"
            style={{ background: toneStyles[credentialTone].soft, borderColor: toneStyles[credentialTone].border }}>
            <span className="mt-0.5 shrink-0" style={{ color: toneStyles[credentialTone].ink }}>
              <CredentialIcon size={22} className={credentialTone === 'pending' ? 'animate-spin' : undefined} />
            </span>
            <div className="min-w-0 space-y-1">
              <p className="font-semibold" style={{ color: toneStyles[credentialTone].ink }}>
                {statusError ? '暂时无法确认凭据状态' : busy || !status ? '正在检查登录凭据…' : titles[status.credentials_state]}
              </p>
              <p className="text-sm text-[var(--text)]">
                {statusError ? '当前状态无法确认，请等待查询恢复。' : status?.message || '在原生 Chrome 登录闲鱼，然后打开或刷新消息页。'}
              </p>
              {status?.captured_at && <p className="text-xs text-[var(--text-muted)]">Token 首次捕获：{new Date(status.captured_at * 1000).toLocaleString()}</p>}
              <p className="text-xs text-[var(--text-muted)]">
                {active ? '停止浏览器不会清除已采用的 Token。' : '每 2 秒自动刷新；保存凭据并关闭闲鱼标签页后，由管家连接验证。'}
              </p>
            </div>
          </div>

          {running && (
            <div className="space-y-3 rounded-[var(--radius-lg)] border border-[var(--border)] p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-semibold">原生 Chrome 桌面</p>
                  <p className="truncate font-mono text-xs text-[var(--text-muted)]" title={desktop?.desktop_url}>{desktop?.desktop_url}</p>
                </div>
                <button type="button" className="ios-btn-primary inline-flex items-center gap-1.5 px-4 py-2" disabled={busy} onClick={() => void openDesktop()}>
                  打开桌面 <ExternalLink size={15} />
                </button>
              </div>
              <ol className="list-decimal space-y-1 pl-5 text-sm text-[var(--text-muted)]">
                <li>点击“打开桌面”，自动授权，无需输入桌面密码（链接一分钟内有效）</li>
                <li>在 Chrome 内用当前账号登录闲鱼</li>
                <li>打开 <span className="font-mono">goofish.com/im</span>，系统自动捕获 Cookie 和消息 Token</li>
              </ol>
              <p className="text-xs text-[var(--text-soft)]">若出现自签名证书提示，请先核对桌面地址。</p>
            </div>
          )}

          <p className="flex gap-2 text-xs leading-relaxed text-[var(--text-soft)]">
            <Info size={14} className="mt-0.5 shrink-0" />
            <span>凭据过期后暂停闲鱼请求。关闭此窗口不会停止监听，也不会提前恢复任务；新凭据保存后先关闭闲鱼标签页，再由管家连接验证。</span>
          </p>
        </div>

        <div className="modal-footer flex flex-wrap items-center gap-2">
          {accountId && (
            <button type="button" className="ios-btn-primary inline-flex items-center gap-1.5 px-4 py-2" disabled={busy} onClick={() => void fetchCredentials()}>
              <RefreshCw size={15} className={fetching ? 'animate-spin' : undefined} />{fetching ? '正在打开消息…' : '获取新凭据'}
            </button>
          )}
          {running ? (
            <button type="button" className="ios-btn-secondary inline-flex items-center gap-1.5 px-4 py-2" disabled={busy} onClick={() => void controlBrowser(false)}>
              <Square size={14} />停止浏览器
            </button>
          ) : (
            <button type="button" className={`${accountId ? 'ios-btn-secondary' : 'ios-btn-primary'} inline-flex items-center gap-1.5 px-4 py-2`} disabled={busy} onClick={() => void controlBrowser(true)}>
              <Play size={14} />启动浏览器
            </button>
          )}
          {running && !listening && (
            <button type="button" className="ios-btn-secondary inline-flex items-center gap-1.5 px-4 py-2" disabled={busy} onClick={() => void reconnect()}>
              <PlugZap size={15} />{reconnecting ? '重新连接中…' : '重新连接监听'}
            </button>
          )}
          <button type="button" disabled={busy || !desktop} onClick={() => void act('clear')}
            className="ml-auto inline-flex items-center gap-1.5 rounded-full px-3 py-2 text-sm text-[var(--danger)] transition hover:bg-[var(--danger-soft)] disabled:cursor-not-allowed disabled:opacity-40">
            <Trash2 size={15} />清空浏览器数据
          </button>
        </div>
      </div>
    </div>, document.body,
  );
}

type Tone = 'ok' | 'warn' | 'error' | 'pending' | 'idle';

const toneStyles: Record<Tone, { ink: string; soft: string; border: string; dot: string }> = {
  ok: { ink: 'var(--success-ink)', soft: 'var(--success-soft)', border: 'color-mix(in srgb, var(--success) 25%, transparent)', dot: 'var(--success)' },
  warn: { ink: 'var(--warning-ink)', soft: 'var(--warning-soft)', border: 'color-mix(in srgb, var(--warning) 25%, transparent)', dot: 'var(--warning)' },
  error: { ink: 'var(--danger-ink)', soft: 'var(--danger-soft)', border: 'color-mix(in srgb, var(--danger) 25%, transparent)', dot: 'var(--danger)' },
  pending: { ink: 'var(--info-ink)', soft: 'var(--info-soft)', border: 'color-mix(in srgb, var(--info) 25%, transparent)', dot: 'var(--info)' },
  idle: { ink: 'var(--text-muted)', soft: 'var(--surface-subtle)', border: 'var(--border)', dot: 'var(--text-soft)' },
};

function StatusTile({ tone, label, value, detail }: { tone: Tone; label: string; value: string; detail: string }) {
  const style = toneStyles[tone];
  return (
    <div role="status" aria-live="polite" className="rounded-[var(--radius-lg)] border border-[var(--border)] bg-[var(--surface-subtle)] px-4 py-3">
      <p className="text-xs text-[var(--text-muted)]">{label}</p>
      <p className="mt-1 flex items-center gap-2 font-semibold" style={{ color: style.ink }}>
        <span className="relative flex h-2 w-2">
          {(tone === 'ok' || tone === 'pending') && <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" style={{ background: style.dot }} />}
          <span className="relative inline-flex h-2 w-2 rounded-full" style={{ background: style.dot }} />
        </span>
        {value}
      </p>
      <p className="mt-0.5 truncate text-xs text-[var(--text-muted)]" title={detail}>{detail}</p>
    </div>
  );
}

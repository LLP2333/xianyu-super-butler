import AccountBrowser from './AccountBrowser';
import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AccountDetail, AIReplySettings } from '../types';
import {
  getAccountDetails,
  updateAccountStatus,
  deleteAccount,
  updateAccountRemark,
  updateAccountAutoConfirm,
  updateAccountPauseDuration,
  updateAccountCookie,
  updateAccountLoginInfo,
  updateAccountAISettings,
  getAllAISettings,
  getAccountAISettings,
  refreshAccountProfile,
  getRiskControlStatus,
} from '../services/api';
import { confirmAction, notify } from '../services/feedback';
import {Power, Edit2, Trash2, X, Check, Loader2, MessageSquare, RefreshCw, Save, User, Clock, Key, Eye, EyeOff, Bot, Settings, MapPin, Users, ShieldCheck} from 'lucide-react';
import { EmptyState, PageHeader, PageLoading } from './ui';

type ModalType = 'edit' | 'ai-settings' | null;

const AccountList: React.FC<{ isAdmin: boolean }> = ({ isAdmin }) => {
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [loading, setLoading] = useState(true);
  // 风控熔断状态：命中后账号会暂停请求，需要让用户看到而不是只报 409
  const [riskBlocked, setRiskBlocked] = useState<Array<{
    cookie_id: string;
    blocked: boolean;
    remaining_seconds: number;
    verification_required?: boolean;
    verification_type: 'none' | 'slider' | 'face' | 'qr' | 'risk_control';
    verification_message: string;
    verification_url?: string;
  }>>([]);
  const [addingAccount, setAddingAccount] = useState(false);
  const setupWindowRef = useRef<Window | null>(null);
  const [activeModal, setActiveModal] = useState<ModalType>(null);
  const [editingAccount, setEditingAccount] = useState<AccountDetail | null>(null);
  const [refreshingProfileId, setRefreshingProfileId] = useState<string | null>(null);
  const [browserAccount, setBrowserAccount] = useState<AccountDetail | null>(null);
  const [failedAvatars, setFailedAvatars] = useState<Set<string>>(new Set());

  // 编辑表单状态
  const [editForm, setEditForm] = useState({
    remark: '',
    cookie: '',
    auto_confirm: false,
    pause_duration: 0,
    username: '',
    login_password: '',
    show_browser: false,
    showLoginPassword: false,
  });

  // AI设置表单状态（字段必须齐全：PUT 是整行 INSERT OR REPLACE，
  // 少一个字段保存时就会被后端默认值静默覆盖）
  const [aiSettings, setAiSettings] = useState<AIReplySettings>({
    ai_enabled: false,
    model_name: 'qwen-plus',
    api_key: '',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    user_agent: '',
    max_discount_percent: 10,
    max_discount_amount: 100,
    max_bargain_rounds: 3,
    context_enabled: true,
    context_message_limit: 12,
    context_expire_minutes: 120,
    custom_prompts: '',
  });
  const [saving, setSaving] = useState(false);

  const loadAccounts = async (options?: { silent?: boolean }) => {
    // 轮询刷新走静默模式，避免每 30 秒把整个列表闪成加载态
    if (!options?.silent) setLoading(true);
    try {
      const data = await getAccountDetails();

      // 获取所有账号的AI设置
      let allAISettings: Record<string, AIReplySettings> = {};
      try {
        allAISettings = await getAllAISettings();
      } catch (e) {
        console.error('Failed to load AI settings:', e);
      }

      // 合并AI设置到账号数据
      const accountsWithAI = data.map(account => ({
        ...account,
        ai_enabled: allAISettings[account.id]?.ai_enabled ?? false,
        max_discount_percent: allAISettings[account.id]?.max_discount_percent ?? 10,
        max_discount_amount: allAISettings[account.id]?.max_discount_amount ?? 100,
        max_bargain_rounds: allAISettings[account.id]?.max_bargain_rounds ?? 3,
        custom_prompts: allAISettings[account.id]?.custom_prompts ?? '',
      }));

      setAccounts(accountsWithAI);
    } catch (error) {
      console.error('Failed to load accounts:', error);
    } finally {
      if (!options?.silent) setLoading(false);
    }
  };

  useEffect(() => {
    const poll = () => {
      getRiskControlStatus()
        .then(res => setRiskBlocked((res.accounts || []).filter(a => a.blocked || (a.verification_type && a.verification_type !== 'none'))))
        .catch(() => setRiskBlocked([]));
    };
    poll();
    const timer = setInterval(poll, 30000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    loadAccounts();
    // 账号的 runtime_state 会随后端连接情况变化（连上、断线重连、进出风控），
    // 只在挂载时拉一次会让页面一直停在旧快照上 —— 表现为账号已在正常收发心跳，
    // 界面却仍显示「未运行」且状态点是灰的。与风控轮询保持同一节奏。
    const timer = setInterval(() => {
      loadAccounts({ silent: true });
    }, 30000);
    return () => {
      clearInterval(timer);
    };
  }, []);

  const handleToggle = async (id: string, currentStatus: boolean) => {
    await updateAccountStatus(id, !currentStatus);
    loadAccounts();
  };

  const handleRefreshProfile = async (id: string) => {
    setRefreshingProfileId(id);
    try {
      await refreshAccountProfile(id);
      setFailedAvatars(previous => {
        const next = new Set(previous);
        next.delete(id);
        return next;
      });
      await loadAccounts();
    } catch (error) {
      console.error('Failed to refresh account profile:', error);
      notify(error instanceof Error ? error.message : '账号资料刷新失败');
    } finally {
      setRefreshingProfileId(null);
    }
  };

  const handleDelete = async (id: string) => {
    if (await confirmAction('确认删除该账号吗？')) {
      await deleteAccount(id);
      loadAccounts();
    }
  };

  const openEditModal = (account: AccountDetail) => {
    setEditingAccount(account);
    setEditForm({
      remark: account.remark || account.note || '',
      cookie: account.cookie || account.value || '',
      auto_confirm: account.auto_confirm || false,
      pause_duration: account.pause_duration || 0,
      username: account.username || '',
      login_password: account.login_password || '',
      show_browser: account.show_browser || false,
      showLoginPassword: false,
    });
    setActiveModal('edit');
  };

  const openAIModal = async (account: AccountDetail) => {
    setEditingAccount(account);
    setSaving(true);
    try {
      const settings = await getAccountAISettings(account.id);
      // 全量加载：PUT 是整行覆盖保存，漏掉的字段（user_agent、
      // context_* 等）会被后端默认值静默重置
      setAiSettings({
        ai_enabled: settings.ai_enabled ?? false,
        model_name: settings.model_name || 'qwen-plus',
        api_key: settings.api_key || '',
        base_url: settings.base_url || 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        user_agent: settings.user_agent ?? '',
        max_discount_percent: settings.max_discount_percent ?? 10,
        max_discount_amount: settings.max_discount_amount ?? 100,
        max_bargain_rounds: settings.max_bargain_rounds ?? 3,
        context_enabled: settings.context_enabled ?? true,
        context_message_limit: settings.context_message_limit ?? 12,
        context_expire_minutes: settings.context_expire_minutes ?? 120,
        custom_prompts: settings.custom_prompts ?? '',
      });
    } catch (e) {
      console.error('Failed to load AI settings:', e);
    } finally {
      setSaving(false);
    }
    setActiveModal('ai-settings');
  };

  const handleSaveEdit = async () => {
    if (!editingAccount) return;
    setSaving(true);

    try {
      const promises: Promise<any>[] = [];

      // 更新备注
      if (editForm.remark !== (editingAccount.remark || editingAccount.note || '')) {
        promises.push(updateAccountRemark(editingAccount.id, editForm.remark));
      }

      // 更新Cookie
      if (editForm.cookie && editForm.cookie !== (editingAccount.cookie || editingAccount.value || '')) {
        promises.push(updateAccountCookie(editingAccount.id, editForm.cookie));
      }

      // 更新自动确认
      if (editForm.auto_confirm !== editingAccount.auto_confirm) {
        promises.push(updateAccountAutoConfirm(editingAccount.id, editForm.auto_confirm));
      }

      // 更新暂停时长
      if (editForm.pause_duration !== (editingAccount.pause_duration || 0)) {
        promises.push(updateAccountPauseDuration(editingAccount.id, editForm.pause_duration));
      }

      // 更新登录信息
      if (
        editForm.username !== (editingAccount.username || '') ||
        editForm.login_password !== (editingAccount.login_password || '') ||
        editForm.show_browser !== (editingAccount.show_browser || false)
      ) {
        promises.push(updateAccountLoginInfo(editingAccount.id, {
          username: editForm.username,
          login_password: editForm.login_password,
          show_browser: editForm.show_browser,
        }));
      }

      await Promise.all(promises);
      setActiveModal(null);
      loadAccounts();
    } catch (error) {
      console.error('更新账号失败:', error);
      notify('更新失败，请重试');
    } finally {
      setSaving(false);
    }
  };

  const handleSaveAISettings = async () => {
    if (!editingAccount) return;
    setSaving(true);

    try {
      await updateAccountAISettings(editingAccount.id, aiSettings);
      setActiveModal(null);
      loadAccounts();
    } catch (error) {
      console.error('更新AI设置失败:', error);
      notify('更新失败，请重试');
    } finally {
      setSaving(false);
    }
  };

  const startBrowserLogin = () => {
    const opened = window.open('about:blank', '_blank');
    if (opened) opened.opener = null;
    setupWindowRef.current = opened;
    setAddingAccount(true);
  };

  const getRuntimeBadge = (account: AccountDetail) => {
    if (!account.enabled) {
      return { label: '已暂停', className: 'bg-gray-100 text-gray-500' };
    }
    if (account.runtime_state === 'waiting_browser') {
      return { label: '等待网页 Token', className: 'bg-amber-100 text-amber-800' };
    }
    if (account.runtime_state === 'running') {
      return { label: '监听中', className: 'bg-green-100 text-green-700' };
    }
    // 登录态过期时，需要在原生浏览器重新登录。
    // 与「连接中」分开显示，否则用户会一直等一个不会变好的状态。
    if (account.runtime_state === 'manual') {
      return { label: '人工操作中', className: 'bg-amber-100 text-amber-800' };
    }
    if (account.runtime_state === 'need_relogin') {
      return { label: '需重新登录', className: 'bg-red-100 text-red-700' };
    }
    if (account.runtime_state === 'failed') {
      return { label: '监听异常', className: 'bg-red-100 text-red-700' };
    }
    // 反复重连通常意味着账号被风控拦住：Token 取不到，消息、发货、自动回复
    // 实际都不通。必须与「监听中」区分开，否则界面会给出一个假的健康状态。
    if (account.runtime_state === 'connecting') {
      return { label: '连接中 / 重连', className: 'bg-amber-100 text-amber-800' };
    }
    return { label: '已启用 / 未运行', className: 'bg-amber-100 text-amber-800' };
  };

  if (loading) return <PageLoading label="正在加载账号和运行状态" />;

  return (
    <div className="page-stack animate-fade-in relative">
      {riskBlocked.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-bold text-amber-900">
            闲鱼要求人机验证，{riskBlocked.length} 个账号无法获取令牌
          </p>
          <p className="mt-1 text-xs leading-5 text-amber-800">
            {riskBlocked.map(item => `${item.cookie_id}：${item.verification_message || '平台风控'}`).join('；')}。
            {/* 只有本地熔断中才有确切的恢复时间；闲鱼侧的验证要求要靠过验证解除，
                写死「N 分钟后恢复」会让用户干等一个不会自动好的状态 */}
            {riskBlocked.some(a => a.verification_required) ? (
              <> 自动重试已停止。请打开原生浏览器，在闲鱼消息页完成验证；捕获 Token 后点「采用网页 Token 并恢复」。</>
            ) : riskBlocked.some(a => a.blocked) ? (
              <>
                {' '}系统会自动退避并重试，预计
                {' '}
                {Math.max(1, Math.ceil(Math.max(...riskBlocked.map(a => a.remaining_seconds || 0)) / 60))}
                {' '}
                分钟后允许重试，是否恢复以 Token 确认结果为准；也可使用下方「打开浏览器」。
              </>
            ) : (
              <>
                {' '}本地冷却已结束，但闲鱼仍要求完成验证。请用下方账号卡片里的
                「打开浏览器」完成网站验证，系统取得 Token 后会恢复账号任务。
              </>
            )}
          </p>
        </div>
      )}

      <PageHeader
        title="账号管理"
        description="管理闲鱼账号授权、监听状态、基础资料和账号级回复策略。"
        icon={Users}
        badge={<span className="status-badge status-badge-info">{accounts.length} 个账号</span>}
        actions={accounts.length === 0 ? (
          <button
            onClick={startBrowserLogin}
            disabled={!isAdmin}
            className="ios-btn-primary flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm"
          >
            <ShieldCheck className="h-4 w-4" />
            浏览器添加账号
          </button>
        ) : undefined}
      />

      <p className="text-xs leading-5 text-gray-400">
        提示：挂 VPN、代理或部署在海外服务器时，闲鱼风控会明显收紧，滑块和人脸验证的通过率大幅下降，建议使用国内网络环境登录与运行。
      </p>

      {/* Account Grid */}
      <div className="grid grid-cols-1 gap-3">
        {accounts.map((account) => {
          const runtimeBadge = getRuntimeBadge(account);
          const isListening = account.enabled && account.runtime_state === 'running';
          const blockedState = riskBlocked.find(item => item.cookie_id === account.id);
          return (
          <article key={account.id} className="ios-card flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex min-w-0 items-start gap-4 sm:items-center">
              <div className="relative">
                {account.avatar_url && !failedAvatars.has(account.avatar_url) ? (
                  <img
                    src={account.avatar_url.startsWith('//') ? `https:${account.avatar_url}` : account.avatar_url}
                    alt=""
                    className="h-14 w-14 rounded-md object-cover"
                    referrerPolicy="no-referrer"
                    loading="lazy"
                    // 按图片地址记录失败，而不是按账号 —— 否则头像换了新地址
                    // 也会一直显示首字母占位，只能靠手动刷新绕过
                    onError={() => setFailedAvatars(previous => new Set(previous).add(account.avatar_url!))}
                  />
                ) : (
                  <div className="flex h-14 w-14 items-center justify-center rounded-md bg-yellow-100 text-lg font-bold text-yellow-800">
                    {account.nickname?.trim().charAt(0) || account.remark?.trim().charAt(0) || '闲'}
                  </div>
                )}
                <div className={`absolute -bottom-1 -right-1 w-6 h-6 rounded-full border-4 border-white flex items-center justify-center ${isListening ? 'bg-green-500' : account.runtime_state === 'failed' ? 'bg-red-500' : 'bg-gray-300'}`}>
                    {isListening && <Check className="w-3 h-3 text-white" />}
                </div>
              </div>
              <div className="min-w-0">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                    <h3 className="break-words text-lg font-bold text-gray-900">{account.nickname || account.remark || '未命名账号'}</h3>
                    <span className={`status-badge ${runtimeBadge.className}`}>
                      {runtimeBadge.label}
                    </span>
                    {account.ai_enabled && (
                        <span className="status-badge status-badge-warning flex items-center gap-1">
                          <Bot className="w-3 h-3" /> AI
                        </span>
                    )}
                </div>
                <p className="break-all font-mono text-xs text-gray-500">闲鱼 ID {account.id}</p>
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-gray-600">
                  {account.location && (
                    <span className="flex items-center gap-1.5">
                      <MapPin className="h-4 w-4 text-gray-400" />
                      {account.location}
                    </span>
                  )}
                  {(account.followers !== undefined || account.following !== undefined) && (
                    <span className="flex items-center gap-1.5">
                      <Users className="h-4 w-4 text-gray-400" />
                      {account.followers?.toLocaleString() ?? '--'} 粉丝
                      <span className="text-gray-400">/</span>
                      {account.following?.toLocaleString() ?? '--'} 关注
                    </span>
                  )}
                </div>
                {account.bio && (
                  <p className="mt-2 max-w-3xl break-words text-sm leading-6 text-gray-600">{account.bio}</p>
                )}
                {account.remark && (
                  <p className="mt-1 text-xs font-medium text-gray-400">备注：{account.remark}</p>
                )}
                <div className="flex flex-wrap gap-2">
                   {blockedState && <span className="status-badge bg-red-100 text-red-700">{blockedState.verification_type === 'slider' ? '滑块验证' : blockedState.verification_type === 'face' ? '人脸验证' : '平台风控'}{blockedState.verification_required ? ' · 等待人工' : blockedState.blocked ? ` · ${Math.max(1, Math.ceil(blockedState.remaining_seconds / 60))} 分钟` : ''}</span>}
                   {account.auto_confirm && <span className="status-badge status-badge-warning flex items-center gap-1.5"><MessageSquare className="w-3 h-3"/> 自动确认</span>}
                   {account.pause_duration > 0 && <span className="status-badge status-badge-info flex items-center gap-1.5"><Clock className="w-3 h-3"/> 暂停 {account.pause_duration} 分钟</span>}
                </div>
                {/* 登录态过期给出明确动作，只挂一个徽标用户不知道该做什么 */}
                {account.runtime_state === 'need_relogin' && (
                  <p className="mt-2 rounded-md bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">
                    闲鱼登录态已过期，请打开下方原生浏览器重新登录
                    使用同一个闲鱼账号登录，捕获网页 Token 后确认恢复。
                  </p>
                )}

              </div>
            </div>
            <div className="flex items-center justify-end gap-1 border-t border-gray-100 pt-3 sm:border-0 sm:pt-0">
                <button
                    onClick={() => setBrowserAccount(account)}
                    disabled={!account.enabled || !isAdmin}
                    className="flex items-center gap-1.5 rounded-md bg-amber-100 px-2.5 py-2 text-xs font-bold text-amber-800 disabled:opacity-40"
                    title={isAdmin ? '打开账号独享浏览器，接管期间暂停自动任务' : '仅管理员可操作服务器浏览器'}
                >
                    <ShieldCheck className="h-4 w-4" />
                    <span>打开浏览器</span>
                </button>
                <button
                    onClick={() => handleRefreshProfile(account.id)}
                    disabled={refreshingProfileId === account.id}
                    className="rounded-md p-2.5 text-amber-700 hover:bg-amber-50 disabled:cursor-wait disabled:opacity-50"
                    title="刷新闲鱼资料"
                    aria-label="刷新闲鱼资料"
                >
                    <RefreshCw className={`w-5 h-5 ${refreshingProfileId === account.id ? 'animate-spin' : ''}`} />
                </button>
                <button
                    onClick={() => openEditModal(account)}
                    className="rounded-md p-2.5 text-gray-600 hover:bg-gray-100"
                    title="编辑账号"
                >
                    <Edit2 className="w-5 h-5" />
                </button>
                <button
                    onClick={() => openAIModal(account)}
                    className="rounded-md p-2.5 text-amber-700 hover:bg-amber-50"
                    title="AI设置"
                >
                    <Bot className="w-5 h-5" />
                </button>
                <button
                    onClick={() => handleToggle(account.id, account.enabled)}
                    className={`rounded-md p-2.5 ${account.enabled ? 'text-green-600 hover:bg-green-50' : 'text-gray-400 hover:bg-gray-100'}`}
                    title={account.enabled ? '暂停账号' : '启用账号'}
                    aria-label={account.enabled ? '暂停账号' : '启用账号'}
                >
                    <Power className="w-5 h-5" />
                </button>
                <button
                    onClick={() => handleDelete(account.id)}
                    className="rounded-md p-2.5 text-red-500 hover:bg-red-100"
                    title="删除账号"
                    aria-label="删除账号"
                >
                    <Trash2 className="w-5 h-5" />
                </button>
            </div>
          </article>
          );
        })}

        {accounts.length === 0 && (
            <EmptyState
              title="暂无闲鱼账号"
              description="在原生浏览器登录闲鱼并打开消息页，系统会自动捕获 Cookie 和消息 Token，添加账号。"
              icon={User}
              action={(
                <button type="button" disabled={!isAdmin} onClick={startBrowserLogin} className="ios-btn-primary rounded-md px-4 py-2 text-sm">
                  浏览器添加账号
                </button>
              )}
            />
        )}
      </div>

      {/* 编辑账号弹窗 */}
      {activeModal === 'edit' && editingAccount && createPortal(
        <div className="modal-overlay">
          <div className="modal-container" style={{maxWidth: '600px'}}>
            <div className="modal-header flex items-start justify-between gap-4">
              <div>
                <h3 className="text-lg font-bold text-gray-900">编辑账号</h3>
                <p className="mt-1 text-xs text-gray-500">{editingAccount.nickname || editingAccount.remark || editingAccount.id}</p>
              </div>
              <button
                type="button"
                onClick={() => setActiveModal(null)}
                className="shrink-0 rounded-md p-2 hover:bg-gray-100"
                aria-label="关闭编辑账号"
              >
                <X className="w-5 h-5 text-gray-500" />
              </button>
            </div>

            <div className="modal-body space-y-5">
              {/* 账号ID */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">账号ID</label>
                <input
                  type="text"
                  value={editingAccount.id}
                  disabled
                  className="ios-input w-full rounded-md bg-gray-50 px-3 py-2.5 text-gray-500"
                />
              </div>

              {/* 备注 */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">备注</label>
                <input
                  type="text"
                  value={editForm.remark}
                  onChange={(e) => setEditForm({ ...editForm, remark: e.target.value })}
                  placeholder="为账号添加备注"
                  className="ios-input w-full rounded-md px-3 py-2.5"
                />
              </div>

              {/* Cookie */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">Cookie</label>
                <textarea
                  value={editForm.cookie}
                  onChange={(e) => setEditForm({ ...editForm, cookie: e.target.value })}
                  placeholder="更新账号Cookie"
                  className="ios-input h-28 w-full resize-y rounded-md px-3 py-2.5 font-mono text-xs"
                />
                <p className="text-xs text-gray-500 mt-1">当前Cookie长度: {editForm.cookie.length} 字符</p>
              </div>

              {/* 自动确认收货 */}
              <div className="flex items-center justify-between gap-4 rounded-md border border-gray-200 bg-gray-50 p-4">
                <div>
                  <div className="font-bold text-gray-900 flex items-center gap-2">
                    <Check className="w-4 h-4 text-green-500" />
                    自动确认收货
                  </div>
                  <div className="text-xs text-gray-500">自动点击确认收货按钮</div>
                </div>
                <button
                  type="button"
                  onClick={() => setEditForm({ ...editForm, auto_confirm: !editForm.auto_confirm })}
                  className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                    editForm.auto_confirm ? 'bg-[#ffe100]' : 'bg-gray-300'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
                      editForm.auto_confirm ? 'translate-x-5' : ''
                    }`}
                  />
                </button>
              </div>

              {/* 暂停时长 */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2 flex items-center gap-2">
                  <Clock className="w-4 h-4 text-blue-500" />
                  暂停处理时长（分钟）
                </label>
                <input
                  type="number"
                  value={editForm.pause_duration}
                  onChange={(e) => setEditForm({ ...editForm, pause_duration: parseInt(e.target.value) || 0 })}
                  placeholder="0"
                  min="0"
                  max="1440"
                  className="ios-input w-full rounded-md px-3 py-2.5"
                />
                <p className="text-xs text-gray-500 mt-1">设置后会暂停处理该账号的订单，到时间后自动恢复</p>
              </div>

              {/* 登录信息 */}
              <div className="border-t border-gray-200 pt-5">
                <h3 className="text-lg font-bold text-gray-900 mb-4 flex items-center gap-2">
                  <Key className="w-5 h-5 text-amber-500" />
                  登录信息
                </h3>
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">用户名</label>
                    <input
                      type="text"
                      value={editForm.username}
                      onChange={(e) => setEditForm({ ...editForm, username: e.target.value })}
                      placeholder="闲鱼账号/手机号"
                      className="ios-input w-full rounded-md px-3 py-2.5"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">登录密码</label>
                    <div className="relative">
                      <input
                        type={editForm.showLoginPassword ? 'text' : 'password'}
                        value={editForm.login_password}
                        onChange={(e) => setEditForm({ ...editForm, login_password: e.target.value })}
                        placeholder="用于自动登录"
                        className="ios-input w-full rounded-md px-3 py-2.5 pr-11"
                      />
                      <button
                        type="button"
                        onClick={() => setEditForm({ ...editForm, showLoginPassword: !editForm.showLoginPassword })}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                      >
                        {editForm.showLoginPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                      </button>
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="font-bold text-gray-900">登录时显示浏览器</div>
                      <div className="text-xs text-gray-500">调试时可开启查看登录过程</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => setEditForm({ ...editForm, show_browser: !editForm.show_browser })}
                      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                        editForm.show_browser ? 'bg-[#ffe100]' : 'bg-gray-300'
                      }`}
                    >
                      <span
                        className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
                          editForm.show_browser ? 'translate-x-5' : ''
                        }`}
                      />
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <div className="modal-footer">
              <div className="flex w-full gap-2">
                <button
                  type="button"
                  onClick={() => setActiveModal(null)}
                  className="ios-btn-secondary flex-1 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={handleSaveEdit}
                  className="ios-btn-primary flex flex-1 items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                  {saving ? '保存中...' : '保存'}
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* AI设置弹窗 */}
      {activeModal === 'ai-settings' && editingAccount && createPortal(
        <div className="modal-overlay">
          <div className="modal-container" style={{maxWidth: '600px'}}>
            <div className="modal-header flex items-start justify-between gap-4">
              <div>
                <h3 className="flex items-center gap-2 text-lg font-bold text-gray-900">
                  <Bot className="h-5 w-5 text-amber-600" />
                  AI 助手设置
                </h3>
                <p className="mt-1 text-xs text-gray-500">{editingAccount.nickname || editingAccount.remark || editingAccount.id}</p>
              </div>
              <button
                type="button"
                onClick={() => setActiveModal(null)}
                className="shrink-0 rounded-md p-2 hover:bg-gray-100"
                aria-label="关闭 AI 助手设置"
              >
                <X className="w-5 h-5 text-gray-500" />
              </button>
            </div>

            <div className="modal-body space-y-6">
              {/* 启用AI */}
              <div className="flex items-center justify-between gap-4 rounded-md border border-gray-200 bg-gray-50 p-4">
                <div>
                  <div className="font-bold text-gray-900 flex items-center gap-2">
                    <Bot className="h-4 w-4 text-amber-600" />
                    启用 AI 自动回复
                  </div>
                  <div className="text-xs text-gray-500">AI将自动处理买家的砍价消息</div>
                </div>
                <button
                  type="button"
                  onClick={() => setAiSettings({ ...aiSettings, ai_enabled: !aiSettings.ai_enabled })}
                  className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                    aiSettings.ai_enabled ? 'bg-[#ffe100]' : 'bg-gray-300'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
                      aiSettings.ai_enabled ? 'translate-x-5' : ''
                    }`}
                  />
                </button>
              </div>

              {/* 砍价策略 */}
              <div className="border-t border-gray-200 pt-6">
                <h3 className="text-lg font-bold text-gray-900 mb-4">砍价策略</h3>
                <div className="grid gap-4 sm:grid-cols-3">
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">最大折扣比例 (%)</label>
                    <input
                      type="number"
                      value={aiSettings.max_discount_percent}
                      onChange={(e) => setAiSettings({ ...aiSettings, max_discount_percent: parseInt(e.target.value) || 0 })}
                      className="ios-input w-full rounded-md px-3 py-2.5"
                      min="0"
                      max="100"
                    />
                    <p className="text-xs text-gray-500 mt-1">例如：10表示最多降价10%</p>
                  </div>
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">最大折扣金额 (元)</label>
                    <input
                      type="number"
                      value={aiSettings.max_discount_amount}
                      onChange={(e) => setAiSettings({ ...aiSettings, max_discount_amount: parseInt(e.target.value) || 0 })}
                      className="ios-input w-full rounded-md px-3 py-2.5"
                      min="0"
                    />
                    <p className="text-xs text-gray-500 mt-1">例如：100表示最多降价100元</p>
                  </div>
                  <div>
                    <label className="block text-sm font-bold text-gray-700 mb-2">最大砍价轮次</label>
                    <input
                      type="number"
                      value={aiSettings.max_bargain_rounds}
                      onChange={(e) => setAiSettings({ ...aiSettings, max_bargain_rounds: parseInt(e.target.value) || 1 })}
                      className="ios-input w-full rounded-md px-3 py-2.5"
                      min="1"
                      max="10"
                    />
                    <p className="text-xs text-gray-500 mt-1">买家最多可以砍价的次数</p>
                  </div>
                </div>
              </div>

              {/* 自定义提示词 */}
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">自定义提示词（可选）</label>
                <textarea
                  value={aiSettings.custom_prompts}
                  onChange={(e) => setAiSettings({ ...aiSettings, custom_prompts: e.target.value })}
                  placeholder="输入自定义的AI回复规则或风格指引...&#10;&#10;例如：回复时保持礼貌专业、使用简洁的语言、强调产品质量等"
                  className="ios-input h-36 w-full resize-y rounded-md px-3 py-2.5"
                />
              </div>

              {/* AI如何工作 */}
              <div className="rounded-md border border-blue-200 bg-blue-50 p-4">
                <h4 className="mb-2 flex items-center gap-2 font-bold text-blue-900">
                  <Settings className="w-4 h-4" />
                  AI如何工作
                </h4>
                <ul className="text-xs text-blue-800 space-y-1">
                  <li>• 自动识别买家的砍价请求</li>
                  <li>• 根据设定的策略智能回复</li>
                  <li>• 在合理范围内同意降价或礼貌拒绝</li>
                  <li>• 保持专业友好的沟通风格</li>
                </ul>
              </div>
            </div>

            <div className="modal-footer">
              <div className="flex w-full gap-2">
                <button
                  type="button"
                  onClick={() => setActiveModal(null)}
                  className="ios-btn-secondary flex-1 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={handleSaveAISettings}
                  className="ios-btn-primary flex flex-1 items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
                  disabled={saving}
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                  {saving ? '保存中...' : '保存'}
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      {addingAccount && <AccountBrowser initialWindow={setupWindowRef.current}
        onClose={() => setAddingAccount(false)} onChanged={() => void loadAccounts({ silent: true })} />}

      {browserAccount && <AccountBrowser account={browserAccount}
        onClose={() => setBrowserAccount(null)}
        onChanged={() => {
          void loadAccounts({ silent: true });
          void getRiskControlStatus().then(data => setRiskBlocked((data.accounts || []).filter(item => item.blocked || (item.verification_type && item.verification_type !== 'none'))));
        }} />}

    </div>
  );
};

export default AccountList;

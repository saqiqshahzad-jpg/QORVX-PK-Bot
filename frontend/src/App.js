/* eslint-disable */
import React, { useState, useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import logoImg from './logo.png';
import { Analytics } from '@vercel/analytics/react';

// --- 🎨 GLOBAL THEME & ACCENT CONFIG ---
const accentColors = {
  emerald: { bg: 'bg-emerald-500', text: 'text-emerald-500', border: 'border-emerald-500', glow: 'bg-emerald-500/20' },
  blue: { bg: 'bg-blue-500', text: 'text-blue-500', border: 'border-blue-500', glow: 'bg-blue-500/20' },
  rose: { bg: 'bg-rose-500', text: 'text-rose-500', border: 'border-rose-500', glow: 'bg-rose-500/20' },
};

// 🛑 API URL Live Backend
const API_URL = 'https://ca-real-estate-ai.onrender.com'; 

export default function App() {
  // --- 🧠 AUTH STATES ---
  const [user, setUser] = useState(null);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [authMode, setAuthMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [otp, setOtp] = useState('');
  const [authError, setAuthError] = useState('');
  const [authSuccess, setAuthSuccess] = useState('');
  const [authLoading, setAuthLoading] = useState(false);

  // --- 📱 MOBILE STATE ---
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);

  // --- 🧠 PASSWORD VALIDATION ---
  const pwdReqs = {
    length: password.length >= 8,
    uppercase: /[A-Z]/.test(password),
    number: /[0-9]/.test(password),
    special: /[^A-Za-z0-9]/.test(password)
  };
  const isPasswordValid = pwdReqs.length && pwdReqs.uppercase && pwdReqs.number && pwdReqs.special;

  // --- 🧠 CHAT STATES ---
  const defaultWelcomeMessage = { role: 'ai', text: "Hi! Welcome to our Premium Real Estate Gateway. Are you looking to find your next dream home, or are you curious about what your current property is worth in today's market? 🏡✨", id: 1 };
  
  const [messages, setMessages] = useState([defaultWelcomeMessage]);
  const [input, setInput] = useState('');
  const [chatHistory, setChatHistory] = useState([]);
  const [activeChatId, setActiveChatId] = useState(null);
  
  const [isTempChat, setIsTempChat] = useState(false);
  const [showTempDisclaimer, setShowTempDisclaimer] = useState(false);
  const [showProfileSettings, setShowProfileSettings] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  
  // 📎 FILE ATTACHMENT STATE
  const [attachedFileData, setAttachedFileData] = useState(null);
  const [attachedFileName, setAttachedFileName] = useState("");

  // 🌓 THEME STATES
  const [isDarkMode, setIsDarkMode] = useState(true);
  const [selectedAccent, setSelectedAccent] = useState('blue');

  const chatContainerRef = useRef(null);
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);

  // --- 💾 INIT ---
  useEffect(() => {
    const savedUser = localStorage.getItem('al_elite_user_final3');
    if (savedUser) setUser(JSON.parse(savedUser));

    const savedData = JSON.parse(localStorage.getItem('al_pro_estate_final3')) || {};
    setChatHistory(savedData.history || []);
    if(savedData.accent) setSelectedAccent(savedData.accent);
    if(savedData.isDarkMode !== undefined) setIsDarkMode(savedData.isDarkMode);
  }, []);

  useEffect(() => {
    if (user && !isTempChat) {
      localStorage.setItem('al_pro_estate_final3', JSON.stringify({
        history: chatHistory,
        accent: selectedAccent,
        isDarkMode: isDarkMode
      }));
    }
  }, [chatHistory, selectedAccent, isDarkMode, user, isTempChat]);

  const scrollToBottom = () => {
    if (chatContainerRef.current) {
      chatContainerRef.current.scrollTo({
        top: chatContainerRef.current.scrollHeight,
        behavior: 'smooth'
      });
    }
  };

  useEffect(() => {
    const timer = setTimeout(scrollToBottom, 100);
    return () => clearTimeout(timer);
  }, [messages, isTyping, attachedFileName]);

  // --- 📎 FILE HANDLING PROTOCOL ---
  const handleFileChange = (e) => {
      const file = e.target.files[0];
      if (!file) return;

      if (file.size > 5 * 1024 * 1024) {
          alert("File is too large. Max size is 5MB.");
          return;
      }

      setAttachedFileName(file.name);
      const reader = new FileReader();
      
      reader.onload = (event) => {
          setAttachedFileData(event.target.result);
      };
      
      if(file.type.includes('text') || file.type.includes('json') || file.name.endsWith('.csv')) {
          reader.readAsText(file);
      } else {
          reader.readAsDataURL(file); 
      }
  };

  const removeAttachment = () => {
      setAttachedFileData(null);
      setAttachedFileName("");
      if(fileInputRef.current) fileInputRef.current.value = "";
  };

  // --- 🔐 AUTH LOGIC ---
  const handleAuth = async (e) => {
    e.preventDefault();
    setAuthError('');
    setAuthSuccess('');
    
    if ((authMode === 'signup' || authMode === 'reset') && !isPasswordValid) {
        setAuthError("Please meet all password requirements.");
        return;
    }

    setAuthLoading(true);

    try {
      if (authMode === 'signup') {
        const res = await fetch(`${API_URL}/signup`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password }),
        });
        const data = await res.json();
        if (res.ok) {
            setAuthMode('otp');
            setAuthSuccess('✅ OTP Sent! Check your email.');
        } else {
            setAuthError(data.detail || "Signup failed");
        }
      } else if (authMode === 'otp') {
        const res = await fetch(`${API_URL}/verify-otp`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, otp }),
          });
          const data = await res.json();
          if (res.ok) {
              setAuthMode('login');
              setAuthSuccess('✅ Verified! Now please login.');
              setOtp('');
              setPassword('');
          } else {
              setAuthError(data.detail || "Invalid OTP");
          }
      } else if (authMode === 'forgot') {
          const res = await fetch(`${API_URL}/forgot-password`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ email }),
          });
          const data = await res.json();
          if(res.ok) {
              setAuthMode('reset');
              setAuthSuccess('✅ Reset Code sent to your email.');
          } else {
              setAuthError(data.detail || "Email not found.");
          }
      } else if (authMode === 'reset') {
          const res = await fetch(`${API_URL}/reset-password`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ email, otp, new_password: password }),
          });
          const data = await res.json();
          if(res.ok) {
              setAuthMode('login');
              setAuthSuccess('✅ Password Reset! Please login.');
              setPassword('');
              setOtp('');
          } else {
              setAuthError(data.detail || "Invalid Code.");
          }
      } else {
        const res = await fetch(`${API_URL}/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password }),
        });
        const data = await res.json();
        if (res.ok) {
            const userData = { email };
            localStorage.setItem('al_elite_user_final3', JSON.stringify(userData));
            
            const savedData = JSON.parse(localStorage.getItem('al_pro_estate_final3')) || {};
            if (savedData.history) {
                setChatHistory(savedData.history);
            }

            setUser(userData);
            setShowAuthModal(false);
            setAuthError('');
            setAuthSuccess('');
            setEmail('');
            setPassword('');
            setMessages([{ role: 'ai', text: `Welcome back, ${email.split('@')[0]}! Feel free to ask any questions.`, id: Date.now() }]);
        } else {
            setAuthError(data.detail || "Login failed");
        }
      }
    } catch (err) {
        setAuthError("⚠️ Connection Error. Contact the Host.");
    }
    setAuthLoading(false);
  };

  const logout = () => {
      setUser(null);
      localStorage.removeItem('al_elite_user_final3');
      setChatHistory([]);
      setMessages([defaultWelcomeMessage]);
      setShowProfileSettings(false);
  };

  const startNewChat = () => {
    setMessages([defaultWelcomeMessage]);
    setActiveChatId(null);
    setIsTempChat(false);
    setShowTempDisclaimer(false);
    setIsSidebarOpen(false);
    removeAttachment();
  };

  const toggleTempChat = () => {
    if (!isTempChat) {
      setShowTempDisclaimer(true); 
    } else {
      setShowTempDisclaimer(false);
      setIsTempChat(false);
      startNewChat();
    }
    setIsSidebarOpen(false);
  };

  const confirmIncognito = () => {
    setIsTempChat(true);
    setShowTempDisclaimer(false);
    setMessages([{ role: 'ai', text: "🕶️ Incognito Mode Activated. Nothing is being saved.", id: Date.now() }]);
    setActiveChatId(null);
  };

  const cancelTempChat = () => {
      setShowTempDisclaimer(false);
  };

  const loadChat = (id) => {
    const chat = chatHistory.find(c => c.id === id);
    if (chat) {
      setMessages(chat.msgs);
      setActiveChatId(id);
      setIsTempChat(false);
      setIsSidebarOpen(false);
    }
  };

  const deleteChat = (id, e) => {
    e.stopPropagation();
    setChatHistory(prev => prev.filter(c => c.id !== id));
    if (activeChatId === id) startNewChat();
  };

  const handleSend = async (overrideText = null) => {
    const isDirectText = typeof overrideText === 'string';
    const textToSend = isDirectText ? overrideText : input;

    if ((!textToSend.trim() && !attachedFileData) || isTyping) return;

    let displayMessageText = textToSend;
    if(attachedFileName && !textToSend.trim()) displayMessageText = `📁 Uploaded Document: ${attachedFileName}`;

    const userMsgId = Date.now();
    const aiMsgId = userMsgId + 1;

    const userMsg = { role: 'user', text: displayMessageText, id: userMsgId };
    
    const currentMemory = messages
        .filter(m => m.id !== 1 && m.text !== '_thinking_')
        .slice(-6)
        .map(m => ({ role: m.role, text: m.text }));

    setMessages(prev => [...prev, userMsg]);
    
    const payloadInputText = textToSend;
    const payloadFileContent = attachedFileData;

    if (!isDirectText) setInput('');
    removeAttachment();
    setIsTyping(true);
    if(window.innerWidth < 768) setIsSidebarOpen(false);

    if (textareaRef.current && !isDirectText) textareaRef.current.style.height = 'auto';

    setMessages(prev => [...prev, { role: 'ai', text: '_thinking_', id: aiMsgId }]);

    try {
      const requestBody = {
          message: payloadInputText || `User uploaded a document for analysis.`,
          email: user ? user.email : "guest",
          client_id: "ca_realestate",
          history: currentMemory 
      };

      if (payloadFileContent) {
          requestBody.file_content = payloadFileContent;
      }

      const response = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
      });
      
      const data = await response.json();
      
      if (!response.ok) {
        throw new Error(data.detail || "Server Error");
      }

      const aiMsg = { role: 'ai', text: data.response || "No response.", id: aiMsgId };
      
      setMessages(prev => prev.map(m => m.id === aiMsgId ? aiMsg : m));

      if (user && !isTempChat) {
        setChatHistory(prevHistory => {
          let updatedHistory;
          const currentChatMsgs = [...messages, userMsg, aiMsg];
          if (!activeChatId) {
            const newId = Date.now() + 2;
            setActiveChatId(newId);
            updatedHistory = [{ id: newId, title: displayMessageText.substring(0, 30) + '...', msgs: currentChatMsgs }, ...prevHistory];
          } else {
            updatedHistory = prevHistory.map(c => c.id === activeChatId ? { ...c, msgs: currentChatMsgs } : c);
          }
          return updatedHistory;
        });
      }
    } catch (e) {
      setMessages(prev => prev.map(m => m.id === aiMsgId ? {
          role: 'ai',
          text: `⚠️ **Connection Failed:** Contact the Host.`,
          id: aiMsgId
      } : m));
    } finally {
      setIsTyping(false);
    }
  };

  // 🧠 THE ULTIMATE FIX: DYNAMIC CONTEXTUAL CHIPS
  const getDynamicChips = () => {
      if (isTyping || messages.length === 0) return [];
      
      const lastMsg = messages[messages.length - 1];
      const lastMsgText = lastMsg.text.toLowerCase();

      // ONLY show for the initial welcome message
      if (lastMsg.role === 'ai' && lastMsgText.includes("welcome to our premium real estate gateway")) {
          return [
              "I want to Buy", 
              "I want to Sell"
          ];
      }

      return [];
  };
  const suggestionChips = getDynamicChips();

  // --- 🎨 THEME ENGINE ---
  const theme = {
    isDarkMode: isDarkMode,
    bg: isDarkMode ? 'bg-[#0a0a0a]' : 'bg-[#eef2f6]',
    textPrimary: isDarkMode ? 'text-white' : 'text-slate-900',
    textSecondary: isDarkMode ? 'text-gray-400' : 'text-slate-600',
    
    sidebarBg: isDarkMode ? 'bg-[#111111]/90' : 'bg-white/90',
    sidebarBorder: isDarkMode ? 'border-white/10' : 'border-slate-300',
    sidebarHover: isDarkMode ? 'hover:bg-white/5' : 'hover:bg-slate-100',
    activeHistory: isDarkMode ? 'bg-white/10 text-white' : 'bg-slate-200 text-slate-900 font-bold',
    
    headerBg: isDarkMode ? 'bg-[#0a0a0a]/80' : 'bg-[#eef2f6]/80',
    
    chatBoxBgAI: isDarkMode ? 'bg-transparent text-gray-200' : 'bg-transparent text-slate-800',
    chatBoxBgUser: isDarkMode ? 'bg-[#222] text-white border-white/5' : 'bg-white text-slate-900 border-slate-300 shadow-md',
    
    inputWrapperBg: isDarkMode ? 'bg-[#1c1c1c]/90' : 'bg-white',
    inputBorder: isDarkMode ? 'border-white/10 focus-within:border-neutral-500' : 'border-slate-300 focus-within:border-slate-500 shadow-md',
    inputAreaGradient: isDarkMode ? 'from-[#0a0a0a] via-[#0a0a0a]/95' : 'from-[#eef2f6] via-[#eef2f6]/95',
    
    blurOrb1: isDarkMode ? 'bg-blue-900/20' : 'bg-blue-300/30',
    blurOrb2: isDarkMode ? 'bg-purple-900/20' : 'bg-purple-300/30',
  };

  const accent = accentColors[selectedAccent];

  return (
    <>
      {/* 🛑 ABSOLUTE LOCK CSS 🛑 */}
      <style dangerouslySetInnerHTML={{__html: `
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        ::selection {
          background: ${isDarkMode ? '#FBFAF5' : '#292929'};
          color: ${isDarkMode ? '#292929' : '#FBFAF5'};
        }
        ::-moz-selection {
          background: ${isDarkMode ? '#FBFAF5' : '#292929'};
          color: ${isDarkMode ? '#292929' : '#FBFAF5'};
        }

        html, body, #root {
            width: 100vw !important;
            height: 100dvh !important;
            overflow: hidden !important;
            position: fixed !important;
            top: 0; left: 0;
            background-color: ${isDarkMode ? '#0a0a0a' : '#eef2f6'};
        }

        @keyframes fadeIn { from { opacity: 0; transform: scale(0.98); } to { opacity: 1; transform: scale(1); } }
        @keyframes slideUp { from { opacity: 0; transform: translateY(15px); } to { opacity: 1; transform: translateY(0); } }
        .animate-fade-in { animation: fadeIn 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
        .animate-slide-up { animation: slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
        
        .markdown-container strong { font-weight: 800; color: inherit; }
        .markdown-container p { margin-bottom: 14px; }
        .markdown-container ul { list-style-type: disc; margin-left: 20px; margin-bottom: 14px; }
        .markdown-container li { margin-bottom: 6px; }
        
        .custom-scrollbar::-webkit-scrollbar { width: 5px; height: 5px;}
        .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #888; border-radius: 10px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #666; }
      `}} />

      <div className={`absolute inset-0 flex overflow-hidden ${theme.textPrimary} transition-colors duration-500 font-sans`}>
        
        <div className={`absolute top-[-10%] left-[-10%] w-[50%] h-[50%] ${theme.blurOrb1} rounded-full blur-[140px] pointer-events-none z-0`}></div>
        <div className={`absolute bottom-[-10%] right-[-10%] w-[60%] h-[60%] ${theme.blurOrb2} rounded-full blur-[160px] pointer-events-none z-0`}></div>

        {isSidebarOpen && <div onClick={() => setIsSidebarOpen(false)} className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden" />}

        {/* SIDEBAR */}
        <div className={`fixed md:relative w-[280px] sm:w-[320px] h-full flex-shrink-0 ${theme.sidebarBg} backdrop-blur-3xl flex flex-col border-r ${theme.sidebarBorder} z-50 transition-transform duration-300 ${isSidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}`}>
          
          <div className="p-5 space-y-4 flex-shrink-0">
            <div className="flex items-center justify-between">
              <h1 className={`text-xl font-extrabold tracking-tighter flex items-center gap-2 ${theme.textPrimary}`}>
                 <img src={logoImg} alt="CA Real Estate Advisor" className="w-12 h-12 rounded-xl object-cover shadow-lg border border-white/10" />
                 CA Real Estate Advisor <span className={`text-[10px] uppercase font-mono px-2 py-0.5 rounded-md text-white shadow-md ${accent.bg}`}>v3.0</span>
              </h1>
              <button onClick={() => setIsSidebarOpen(false)} className="md:hidden opacity-50 p-2"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button>
            </div>
            
            <button onClick={startNewChat} className={`w-full flex items-center gap-3 rounded-xl p-3.5 text-sm font-bold transition active:scale-95 border ${theme.sidebarBorder} ${theme.sidebarHover} ${theme.textPrimary} shadow-sm`}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M12 5v14M5 12h14"/></svg>
              New Conversation
            </button>

            {user && (
              <button onClick={toggleTempChat} className={`w-full flex items-center justify-between rounded-xl p-3.5 text-sm transition group border ${isTempChat ? `${accent.glow} ${accent.text} border-current` : `${theme.sidebarHover} border-transparent ${theme.textSecondary}`}`}>
                 <span className="flex items-center gap-3">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                  Incognito Mode
                 </span>
                 <div className={`w-9 h-5 rounded-full p-0.5 transition ${isTempChat ? accent.bg : (isDarkMode ? 'bg-neutral-700' : 'bg-slate-300')}`}>
                    <div className={`w-4 h-4 bg-white rounded-full transition-transform duration-200 ${isTempChat ? 'translate-x-4' : 'translate-x-0'}`} />
                 </div>
              </button>
            )}
          </div>

          <div className="px-5 mb-4 flex-shrink-0">
              {!user ? (
                  <div className={`bg-gradient-to-br ${isDarkMode ? 'from-blue-900/30 to-purple-900/30 border-blue-500/20' : 'from-blue-200/50 to-purple-200/50 border-blue-300'} border rounded-2xl p-6 text-center shadow-lg backdrop-blur-md relative overflow-hidden`}>
                      <p className={`text-sm font-extrabold mb-2 ${theme.textPrimary}`}>Unlock Features</p>
                      <p className={`text-xs mb-5 leading-relaxed ${theme.textSecondary}`}>Log in to save history and customize your AI.</p>
                      <button onClick={() => {setAuthMode('login'); setShowAuthModal(true);}} className={`w-full ${isDarkMode ? 'bg-white text-black' : 'bg-slate-900 text-white'} font-extrabold py-3 rounded-xl text-sm hover:scale-105 transition active:scale-95 shadow-md`}>
                          Sign In / Sign Up
                      </button>
                  </div>
              ) : (
                <div className="relative">
                  <svg className={`absolute left-4 top-3.5 ${theme.textSecondary}`} width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
                  <input type="text" placeholder="Search history..." className={`w-full ${isDarkMode ? 'bg-black/30' : 'bg-white/60'} rounded-xl pl-10 pr-4 py-3 text-xs border ${theme.sidebarBorder} outline-none focus:${accent.border} transition shadow-inner ${theme.textPrimary} placeholder:${theme.textSecondary}`} onChange={(e) => setSearchQuery(e.target.value)} />
                </div>
              )}
          </div>

          <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-1.5 custom-scrollbar min-h-0">
            {user ? (
                <>
                    <p className={`text-[10px] ${theme.textSecondary} font-bold uppercase tracking-widest px-2 mb-2 sticky top-0 bg-transparent backdrop-blur-sm py-1`}>Recent Chats</p>
                    {chatHistory.filter(c => c.title.toLowerCase().includes(searchQuery.toLowerCase())).map(chat => (
                      <div key={chat.id} onClick={() => loadChat(chat.id)} className={`p-3 rounded-xl cursor-pointer text-sm transition group flex items-center gap-3 ${activeChatId === chat.id ? theme.activeHistory : `${theme.sidebarHover} ${theme.textSecondary} border border-transparent`}`}>
                        <svg className="flex-shrink-0" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                        <span className="truncate flex-1 font-medium">{chat.title}</span>
                        {activeChatId === chat.id && (
                          <button onClick={(e) => deleteChat(chat.id, e)} className="text-gray-400 hover:text-red-500 p-1 opacity-0 group-hover:opacity-100 transition-opacity">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                          </button>
                        )}
                      </div>
                    ))}
                    {chatHistory.length === 0 && <div className="text-center mt-10"><p className={`text-[10px] font-bold uppercase tracking-widest ${theme.textSecondary}`}>No History</p></div>}
                </>
            ) : (
                <div className={`flex flex-col items-center justify-center h-full opacity-50 px-4 text-center ${theme.textSecondary}`}>
                    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" className="mb-4"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                    <p className="text-xs font-semibold">History disabled, login to enable</p>
                </div>
            )}
          </div>

          <div className={`p-4 border-t ${theme.sidebarBorder} relative bg-transparent flex-shrink-0`}>
            {showProfileSettings && user && (
              <div className={`absolute bottom-20 left-4 w-[270px] ${isDarkMode ? 'bg-[#1a1a1a]' : 'bg-white'} backdrop-blur-3xl rounded-3xl shadow-2xl border ${theme.sidebarBorder} overflow-hidden animate-slide-up z-50 p-2`}>
                <div className={`p-4 border-b ${theme.sidebarBorder} flex items-center gap-4`}>
                  <div className={`w-12 h-12 rounded-full ${accent.bg} text-white flex items-center justify-center font-extrabold text-xl shadow-inner`}>{user.email.charAt(0).toUpperCase()}</div>
                  <div className="overflow-hidden">
                      <p className={`text-sm font-extrabold truncate ${theme.textPrimary}`}>{user.email}</p>
                      <span className={`text-[9px] px-2 py-0.5 rounded-md ${accent.glow} ${accent.text} mt-1 inline-block font-bold tracking-widest border border-current/20`}>VERIFIED</span>
                  </div>
                </div>
                
                <div className="p-3 space-y-4">
                  <div className="flex items-center justify-between px-1">
                    <span className={`text-xs font-bold ${theme.textSecondary}`}>Theme Mode</span>
                    <button onClick={() => setIsDarkMode(!isDarkMode)} className={`px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-2 ${isDarkMode ? 'bg-yellow-500/10 text-yellow-500 hover:bg-yellow-500/20' : 'bg-indigo-500/10 text-indigo-600 hover:bg-indigo-100'}`}>
                      {isDarkMode ? '💡 Light' : '🌙 Dark'}
                    </button>
                  </div>

                  <div>
                      <p className={`text-[10px] ${theme.textSecondary} font-bold uppercase tracking-widest px-1 mb-2`}>Color Accent</p>
                      <div className={`flex gap-3 p-3 rounded-2xl ${isDarkMode ? 'bg-[#2a2a2a]/50' : 'bg-slate-100'} border ${theme.sidebarBorder} justify-center`}>
                          {Object.keys(accentColors).map(key => (
                              <button key={key} onClick={() => setSelectedAccent(key)} className={`w-8 h-8 rounded-full border-2 transition-all hover:scale-110 shadow-md ${selectedAccent === key ? (isDarkMode ? 'border-white scale-110' : 'border-slate-800 scale-110') : 'border-transparent opacity-50'} ${accentColors[key].bg}`}></button>
                          ))}
                      </div>
                  </div>

                  <button onClick={logout} className={`w-full text-left p-3.5 hover:bg-red-500/10 text-red-500 rounded-xl text-sm transition font-bold flex items-center gap-3 border border-transparent hover:border-red-500/20`}>
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M19 12H5m14 0-4 4m4-4-4-4"/></svg> Secure Logout
                  </button>
                </div>
              </div>
            )}
            
            {user ? (
              <button onClick={() => setShowProfileSettings(!showProfileSettings)} className={`w-full flex items-center gap-4 p-2.5 rounded-2xl transition ${theme.sidebarHover} border border-transparent shadow-sm group`}>
                  <div className={`w-10 h-10 rounded-full ${accent.bg} text-white flex items-center justify-center font-extrabold text-sm group-hover:scale-105 transition shadow-md`}>{user.email.charAt(0).toUpperCase()}</div>
                  <div className="flex-1 text-left overflow-hidden">
                      <span className={`text-sm font-bold leading-tight block truncate ${theme.textPrimary}`}>{user.email}</span>
                      <span className={`text-[10px] ${theme.textSecondary} font-medium`}>Settings & Theme</span>
                  </div>
              </button>
            ) : (
               <div className="text-center w-full">
                  <button onClick={() => {setAuthMode('login'); setShowAuthModal(true);}} className={`w-full ${isDarkMode ? 'bg-white text-black' : 'bg-slate-900 text-white'} font-extrabold py-3.5 rounded-xl text-sm hover:opacity-80 transition active:scale-95 shadow-md`}>
                      Sign In / Sign Up
                  </button>
               </div>
            )}
          </div>
        </div>

        {/* MAIN CHAT AREA */}
        <div className="flex-1 flex flex-col h-full relative z-10 min-w-0 overflow-hidden">
          
          <header className={`h-16 border-b ${theme.sidebarBorder} flex items-center justify-between px-6 md:px-8 ${theme.headerBg} backdrop-blur-xl z-30 flex-shrink-0`}>
            <div className="flex items-center gap-3">
              <button onClick={() => setIsSidebarOpen(true)} className="md:hidden p-2 -ml-2 rounded-lg hover:bg-white/5 transition"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M3 12h18M3 6h18M3 18h18"/></svg></button>
              <span className={`text-[12px] md:text-sm font-extrabold tracking-widest uppercase flex items-center gap-3 ${theme.textPrimary}`}>
                 CA Real Estate ADVISOR
                 <span className={`w-2 h-2 md:w-2.5 md:h-2.5 rounded-full ${accent.bg} animate-pulse`}></span>
              </span>
            </div>
            {isTempChat && (
              <span className="text-[9px] md:text-[10px] bg-indigo-500/10 text-indigo-500 px-2 md:px-3 py-1.5 rounded-full border border-indigo-500/30 font-bold flex items-center shadow-sm">
                 INCOGNITO ON
              </span>
            )}
          </header>

          <div ref={chatContainerRef} className="flex-1 overflow-y-auto custom-scrollbar p-4 md:p-10">
            <div className="max-w-4xl mx-auto w-full flex flex-col gap-6 pb-4">
              
              {messages.length === 0 && (
                 <div className="flex flex-col items-center justify-center mt-20 opacity-80 select-none px-4 text-center animate-fade-in">
                    <div className={`w-24 h-24 rounded-full flex items-center justify-center mb-6 border ${theme.sidebarBorder} shadow-2xl backdrop-blur-xl ${isDarkMode ? 'bg-white/5' : 'bg-black/5'}`}>
                        <svg className={theme.textSecondary} width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
                    </div>
                    <h1 className={`text-3xl md:text-4xl font-extrabold mt-4 tracking-tighter ${theme.textPrimary} mb-3`}>Ready to Assist.</h1>
                    <p className={`text-sm ${theme.textSecondary}`}>Ask questions or upload your property documents for analysis.</p>
                 </div>
              )}

              {messages.map((msg, i) => (
                <div key={msg.id || i} className={`flex gap-3 md:gap-5 group animate-fade-in ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
                  <div className={`w-8 h-8 md:w-10 md:h-10 rounded-xl flex items-center justify-center font-bold text-[10px] md:text-[11px] flex-shrink-0 shadow-md mt-1 border ${msg.role === 'ai' ? `${accent.bg} text-white border-transparent` : (isDarkMode ? 'bg-neutral-800 text-white border-neutral-600' : 'bg-white text-slate-800 border-slate-300 shadow-sm')}`}>
                    {msg.role === 'ai' ? 'AI' : (user ? user.email.charAt(0).toUpperCase() : 'U')}
                  </div>
                  
                  <div className={`max-w-[90%] md:max-w-[85%] text-[14px] md:text-[15.5px] leading-relaxed markdown-container break-words border ${msg.role === 'user' ? `${theme.chatBoxBgUser} px-4 md:px-6 py-3 md:py-4 rounded-2xl md:rounded-3xl rounded-tr-sm` : `${theme.chatBoxBgAI} ${isDarkMode ? 'bg-[#1c1c1c]/40' : 'bg-white/80'} backdrop-blur-2xl p-4 md:p-6 rounded-2xl md:rounded-3xl rounded-tl-sm shadow-lg`}`}>
                      {msg.text === '_thinking_' ? (
                          <div className="flex gap-2 items-center h-6 animate-pulse">
                              <div className={`w-2.5 h-2.5 rounded-full ${isDarkMode ? 'bg-white' : 'bg-slate-800'} animate-bounce`}></div>
                              <div className={`w-2.5 h-2.5 rounded-full ${isDarkMode ? 'bg-white' : 'bg-slate-800'} animate-bounce delay-75`}></div>
                              <div className={`w-2.5 h-2.5 rounded-full ${isDarkMode ? 'bg-white' : 'bg-slate-800'} animate-bounce delay-150`}></div>
                          </div>
                      ) : (
                          <ReactMarkdown>{msg.text}</ReactMarkdown>
                      )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* INPUT AREA WITH ATTACHMENT */}
          <div className={`p-4 md:p-6 lg:pb-8 bg-gradient-to-t ${theme.inputAreaGradient} flex-shrink-0 z-20`}>
            <div className="max-w-4xl mx-auto relative group">
              
              {/* FILE PREVIEW BAR */}
              {attachedFileName && (
                  <div className={`mb-3 flex justify-between items-center px-4 py-2 rounded-xl text-[12px] font-bold border ${accent.border} ${accent.glow} ${accent.text}`}>
                      <span className="truncate">📁 {attachedFileName}</span>
                      <button onClick={removeAttachment} className="ml-3 hover:text-red-500 font-extrabold text-sm">✖</button>
                  </div>
              )}

              {/* 🧠 THE ULTIMATE FIX: DYNAMIC SUGGESTION CHIPS (CLICK TO SEND) */}
              {suggestionChips.length > 0 && (
                  <div className="flex flex-wrap gap-3 mb-4 justify-center md:justify-start animate-fade-in">
                      {suggestionChips.map((chip, idx) => (
                          <button
                              key={idx}
                              onClick={() => handleSend(chip)}
                              className={`px-5 py-2.5 rounded-full text-sm font-bold border transition-all active:scale-95 shadow-md flex items-center gap-2 ${accent.bg} text-white border-transparent hover:opacity-80`}
                          >
                              {chip}
                          </button>
                      ))}
                  </div>
              )}

              <div className={`${theme.inputWrapperBg} backdrop-blur-3xl rounded-3xl border ${theme.inputBorder} transition-all p-2 flex items-end shadow-2xl`}>
                
                {/* FILE UPLOAD BUTTON */}
                <input type="file" id="al-file-injector" ref={fileInputRef} className="hidden" onChange={handleFileChange} accept=".txt,.json,.csv,.pdf,.png,.jpg,.jpeg" />
                <label htmlFor="al-file-injector" className={`p-3 md:p-4 mr-1 mb-1 rounded-xl flex items-center justify-center cursor-pointer transition-all ${attachedFileData ? `${accent.bg} text-white shadow-md` : `bg-transparent ${theme.textSecondary} hover:${theme.textPrimary}`}`}>
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>
                </label>

                <textarea
                  ref={textareaRef}
                  rows="1"
                  className={`flex-1 bg-transparent ${theme.textPrimary} p-3 md:p-4 outline-none resize-none max-h-32 md:max-h-48 text-[14px] md:text-[15px] custom-scrollbar placeholder:${theme.textSecondary}`}
                  placeholder="Ask a question or upload a document..."
                  value={input}
                  onChange={(e) => {
                      setInput(e.target.value);
                      e.target.style.height = 'auto';
                      e.target.style.height = Math.min(e.target.scrollHeight, 150) + 'px';
                  }}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
                />
                <button
                  onClick={() => handleSend()}
                  disabled={(!input.trim() && !attachedFileData) || isTyping}
                  className={`p-3 md:p-4 rounded-xl md:rounded-2xl mb-1 mr-1 transition-all flex items-center justify-center ${ (input.trim() || attachedFileData) && !isTyping ? `${accent.bg} text-white shadow-lg hover:scale-105 active:scale-95` : `bg-transparent ${theme.textSecondary} cursor-not-allowed`}`}
                >
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
                </button>
              </div>
              <p className={`text-center text-[10px] md:text-[11px] ${theme.textSecondary} mt-3 font-semibold tracking-[1px]`}>
                AI can make mistakes. Verify important information.
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* AUTH MODAL */}
      {showAuthModal && !user && (
         <div className="fixed inset-0 bg-black/80 backdrop-blur-2xl z-[200] flex items-center justify-center p-4 animate-fade-in overflow-y-auto">
           <div className={`absolute top-[10%] left-[20%] w-[400px] h-[400px] ${theme.blurOrb1} rounded-full blur-[150px] pointer-events-none`}></div>
           
           <div className={`w-full max-w-md my-auto ${isDarkMode ? 'bg-[#111111]/95' : 'bg-white/95'} backdrop-blur-3xl p-6 md:p-10 rounded-[2rem] border ${theme.sidebarBorder} shadow-2xl relative z-10 animate-slide-up`}>
              <button onClick={() => setShowAuthModal(false)} className={`absolute top-6 right-6 ${theme.textSecondary} hover:${theme.textPrimary} transition ${theme.sidebarHover} p-2 rounded-full`}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
              </button>
              
              <div className={`w-14 h-14 md:w-16 md:h-16 ${isDarkMode ? 'bg-white/5' : 'bg-slate-100'} rounded-2xl flex items-center justify-center mx-auto mb-6 border ${theme.sidebarBorder} shadow-sm`}>
                  <svg className={theme.textPrimary} width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
              </div>

              <h2 className={`text-2xl md:text-3xl font-extrabold mb-2 text-center tracking-tight ${theme.textPrimary}`}>
                 {authMode === 'forgot' || authMode === 'reset' ? 'Account Recovery' : 'Elite Access'}
              </h2>
              <p className={`text-center mb-6 md:mb-8 text-xs md:text-sm ${theme.textSecondary}`}>
                {authMode === 'login' ? 'Welcome back! Sign in to continue.' :
                 authMode === 'signup' ? 'Create a secure account.' :
                 authMode === 'forgot' ? 'Enter email to receive reset code.' :
                 authMode === 'reset' ? 'Enter new password and OTP.' :
                 'Check your email for the verification code.'}
              </p>

              <form onSubmit={handleAuth} className="space-y-4 md:space-y-5">
                  {(authMode === 'login' || authMode === 'signup' || authMode === 'forgot' || authMode === 'reset') && (
                      <div>
                          <label className={`text-[10px] ${theme.textSecondary} font-bold uppercase tracking-widest mb-2 block`}>Email Address</label>
                          <input type="email" required disabled={authMode === 'reset'} className={`w-full ${theme.inputWrapperBg} border ${theme.sidebarBorder} rounded-xl p-3 md:p-4 outline-none focus:${accent.border} transition ${theme.textPrimary} shadow-inner text-sm disabled:opacity-50`} value={email} onChange={(e) => setEmail(e.target.value)} />
                      </div>
                  )}

                  {(authMode === 'login' || authMode === 'signup' || authMode === 'reset') && (
                      <div className="relative">
                          <label className={`text-[10px] ${theme.textSecondary} font-bold uppercase tracking-widest mb-2 block`}>
                              {authMode === 'reset' ? 'New Password' : 'Password'}
                          </label>
                          <input
                              type={showPassword ? "text" : "password"}
                              required
                              className={`w-full ${theme.inputWrapperBg} border ${theme.sidebarBorder} rounded-xl p-3 md:p-4 pr-12 outline-none focus:${accent.border} transition ${theme.textPrimary} shadow-inner text-sm`}
                              value={password}
                              onChange={(e) => setPassword(e.target.value)}
                          />
                          <button
                              type="button"
                              onClick={() => setShowPassword(!showPassword)}
                              className={`absolute right-4 top-[36px] md:top-[38px] ${theme.textSecondary} hover:${theme.textPrimary} transition focus:outline-none`}
                          >
                              {showPassword ?
                                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                                  :
                                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                              }
                          </button>
                      </div>
                  )}

                  {authMode === 'login' && (
                     <div className="text-right">
                         <button type="button" onClick={() => {setAuthMode('forgot'); setAuthError(''); setAuthSuccess('');}} className={`text-[11px] ${accent.text} font-bold hover:underline`}>
                             Forgot Password?
                         </button>
                     </div>
                  )}
                  
                  {(authMode === 'signup' || authMode === 'reset') && (
                      <div className={`p-3 md:p-4 rounded-xl border ${isDarkMode ? 'bg-white/5 border-white/10' : 'bg-slate-100 border-slate-300'} space-y-2 mt-2 transition-all`}>
                          <p className={`text-[9px] md:text-[10px] font-bold uppercase tracking-widest ${theme.textPrimary} mb-2`}>Password Requirements:</p>
                          <div className="flex items-center gap-2 text-[11px] md:text-xs font-medium">
                              {pwdReqs.length ? <span className="text-emerald-500 scale-110 transition-transform">✅</span> : <span className={theme.textSecondary}>⚪</span>}
                              <span className={pwdReqs.length ? theme.textPrimary : theme.textSecondary}>At least 8 characters</span>
                          </div>
                          <div className="flex items-center gap-2 text-[11px] md:text-xs font-medium">
                              {pwdReqs.uppercase ? <span className="text-emerald-500 scale-110 transition-transform">✅</span> : <span className={theme.textSecondary}>⚪</span>}
                              <span className={pwdReqs.uppercase ? theme.textPrimary : theme.textSecondary}>One uppercase letter</span>
                          </div>
                          <div className="flex items-center gap-2 text-[11px] md:text-xs font-medium">
                              {pwdReqs.number ? <span className="text-emerald-500 scale-110 transition-transform">✅</span> : <span className={theme.textSecondary}>⚪</span>}
                              <span className={pwdReqs.number ? theme.textPrimary : theme.textSecondary}>One number</span>
                          </div>
                          <div className="flex items-center gap-2 text-[11px] md:text-xs font-medium">
                              {pwdReqs.special ? <span className="text-emerald-500 scale-110 transition-transform">✅</span> : <span className={theme.textSecondary}>⚪</span>}
                              <span className={pwdReqs.special ? theme.textPrimary : theme.textSecondary}>One special char (!@#$)</span>
                          </div>
                      </div>
                  )}

                  {(authMode === 'otp' || authMode === 'reset') && (
                       <div>
                          <label className={`text-[10px] ${theme.textSecondary} font-bold uppercase tracking-widest mb-2 block`}>Verification Code (OTP)</label>
                          <input type="text" required maxLength="6" className={`w-full ${theme.inputWrapperBg} border ${theme.sidebarBorder} rounded-xl p-4 outline-none focus:${accent.border} transition text-center tracking-[10px] md:tracking-[15px] text-xl md:text-2xl font-mono ${theme.textPrimary} shadow-inner`} value={otp} onChange={e => setOtp(e.target.value)} />
                      </div>
                  )}

                  {authError && <p className="text-red-500 text-[10px] md:text-xs text-center font-bold bg-red-500/10 py-2 md:py-2.5 rounded-lg border border-red-500/20"><ReactMarkdown>{authError}</ReactMarkdown></p>}
                  {authSuccess && <p className="text-emerald-500 text-[10px] md:text-xs text-center font-bold bg-emerald-500/10 py-2 md:py-2.5 rounded-lg border border-emerald-500/20">{authSuccess}</p>}

                  <button
                      type="submit"
                      disabled={authLoading || ((authMode === 'signup' || authMode === 'reset') && !isPasswordValid)}
                      className={`w-full ${accent.bg} text-white font-extrabold py-3 md:py-4 rounded-xl transition active:scale-95 mt-2 shadow-[0_0_20px_rgba(0,0,0,0.2)] hover:opacity-90 text-sm md:text-base disabled:opacity-50 disabled:cursor-not-allowed`}
                  >
                      {authLoading ? 'Processing...' : (authMode === 'login' ? 'Secure Login' : authMode === 'signup' ? 'Create Account' : authMode === 'forgot' ? 'Send Reset Link' : 'Verify & Continue')}
                  </button>
              </form>

              <div className="mt-6 text-center text-[11px] md:text-xs font-medium">
                  {authMode === 'login' ? (
                      <p className={theme.textSecondary}>New here? <button onClick={() => {setAuthMode('signup'); setAuthError(''); setAuthSuccess(''); setPassword('');}} className={`${accent.text} hover:opacity-80 font-bold ml-1`}>Create an account</button></p>
                  ) : (authMode === 'signup' || authMode === 'forgot' || authMode === 'reset') ? (
                      <p className={theme.textSecondary}>Remembered password? <button onClick={() => {setAuthMode('login'); setAuthError(''); setAuthSuccess(''); setPassword(''); setOtp('');}} className={`${accent.text} hover:opacity-80 font-bold ml-1`}>Sign in</button></p>
                  ) : (
                      <p className={theme.textSecondary}>OTP is sent via secure SMTP. Check spam if not found.</p>
                  )}
              </div>
           </div>
         </div>
      )}

      {/* INCOGNITO DISCLAIMER */}
      {showTempDisclaimer && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-xl z-[100] flex items-center justify-center p-6 animate-fade-in">
          <div className="absolute inset-0 bg-indigo-950/20 w-[300px] h-[300px] md:w-[400px] md:h-[400px] blur-[150px] mx-auto my-auto rounded-full pointer-events-none"></div>
          <div className={`bg-[#111111]/90 backdrop-blur-lg border border-indigo-700/50 p-6 md:p-10 rounded-3xl max-w-lg text-center shadow-2xl relative z-10 border-indigo-900 border-4`}>
            
            {/* The Cancel Button */}
            <button onClick={cancelTempChat} className={`absolute top-4 right-5 text-indigo-400 hover:text-white transition p-2 rounded-full font-extrabold text-xl`}>
                ✖
            </button>

            <div className="w-20 h-20 md:w-24 md:h-24 bg-indigo-500/20 text-indigo-400 rounded-full flex items-center justify-center mx-auto mb-6 md:mb-8 border border-indigo-700/40 animate-pulse">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M2 12h3M19 12h3M12 2v3M12 19v3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/></svg>
            </div>
            <h2 className="text-2xl md:text-3xl font-extrabold mb-3 md:mb-5 text-white tracking-tighter">Incognito Mode</h2>
            <p className="text-indigo-200 text-sm md:text-md leading-relaxed mb-8 md:mb-10 tracking-tight">
              You are entering **Incognito Mode**. Messages will not be saved to history and will vanish when you leave.
            </p>
            {/* The Confirm Button */}
            <button onClick={confirmIncognito} className="w-full bg-white hover:bg-neutral-200 text-black font-extrabold py-3 md:py-4 rounded-xl transition active:scale-95 shadow-xl text-sm md:text-lg">
              I Understand
            </button>
          </div>
        </div>
      )}
      <Analytics />
    </>
  );
}

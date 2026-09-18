import { useState, useMemo } from 'react';
import { ChevronDown, ChevronRight, Clock, CheckCircle, XCircle, Copy, Check, Wrench, MessageSquare, Code, X, Maximize2 } from 'lucide-react';
import type { ExecutionTrace, ToolCallTrace, TraceMessage } from '../../types';
import { Card } from '../common/Card';
import { Badge } from '../common/Badge';

interface ExecutionTracePanelProps {
  trace: ExecutionTrace;
}

// Format milliseconds to readable string
function formatMs(ms: number): string {
  if (ms < 0 || ms > 1000000000) return '—'; // Invalid or unreasonably large
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

// Parse AgentCore prompt format which may be escaped JSON strings
// Format: "{'content': '[{\"text\": \"actual content...\"}]'}" or "[{\"text\": \"...\"}]"
function parseAgentCoreContent(content: string): string {
  if (!content) return '';

  try {
    // Handle Python-style dict wrapper: {'content': '[...]'}
    // This format has single quotes around keys and the outer value
    if (content.startsWith("{'content':") || content.startsWith("{\"content\":")) {
      // Extract the inner content using regex
      // Match: {'content': '...'} or {"content": "..."}
      const match = content.match(/['"]content['"]\s*:\s*['"](.+)['"]\s*\}$/s);
      if (match) {
        let innerContent = match[1];
        // Unescape the inner content (handle escaped quotes and backslashes)
        innerContent = innerContent
          .replace(/\\'/g, "'")
          .replace(/\\"/g, '"')
          .replace(/\\\\/g, '\\');
        return parseAgentCoreContent(innerContent);
      }

      // Alternative: try to extract JSON array from the string
      const jsonArrayMatch = content.match(/\[[\s\S]*\]/);
      if (jsonArrayMatch) {
        return parseAgentCoreContent(jsonArrayMatch[0]);
      }
    }

    // Try to parse as JSON array: [{"text": "..."}]
    if (content.startsWith('[')) {
      const parsed = JSON.parse(content);
      if (Array.isArray(parsed)) {
        const texts = parsed
          .map((item: { text?: string; reasoningContent?: { reasoningText?: { text?: string } } }) => {
            // Handle regular text items
            if (item.text) return item.text;
            // Handle reasoning content (from assistant messages)
            if (item.reasoningContent?.reasoningText?.text) {
              return item.reasoningContent.reasoningText.text;
            }
            return '';
          })
          .filter(Boolean);
        if (texts.length > 0) {
          return texts.join('\n\n');
        }
      }
    }

    // Try to parse as JSON object with message field
    if (content.startsWith('{')) {
      const parsed = JSON.parse(content);
      if (parsed.message) {
        return parseAgentCoreContent(parsed.message);
      }
      if (parsed.content) {
        return parseAgentCoreContent(parsed.content);
      }
      if (parsed.text) {
        return parsed.text;
      }
    }

    // Return as-is if no parsing worked
    return content;
  } catch {
    // If all parsing fails, try to unescape common escape sequences
    return content
      .replace(/\\\\n/g, '\n')
      .replace(/\\n/g, '\n')
      .replace(/\\\\"/g, '"')
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, '\\');
  }
}

// Format message content for display
function formatMessageContent(msg: TraceMessage): string {
  if (!msg.content) return '';

  // If content is a string, parse it
  if (typeof msg.content === 'string') {
    return parseAgentCoreContent(msg.content);
  }

  // If content is an object with content or message field (AgentCore format)
  if (typeof msg.content === 'object') {
    const contentObj = msg.content as { content?: string; message?: string };
    if (contentObj.content) {
      return parseAgentCoreContent(contentObj.content);
    }
    if (contentObj.message) {
      return parseAgentCoreContent(contentObj.message);
    }
  }

  // If content is an array (standard format), extract text
  if (Array.isArray(msg.content)) {
    return msg.content
      .map(c => {
        if (c.text) return c.text;
        if (c.toolUse) return `[Tool Use: ${c.toolUse.name || 'unknown'}]`;
        if (c.toolResult) return `[Tool Result]`;
        return '';
      })
      .filter(Boolean)
      .join('\n');
  }

  return JSON.stringify(msg.content, null, 2);
}

export function ExecutionTracePanel({ trace }: ExecutionTracePanelProps) {
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set(['timing']));
  const [copiedSection, setCopiedSection] = useState<string | null>(null);
  const [modalContent, setModalContent] = useState<{ title: string; content: string } | null>(null);

  // Determine if this is an AgentCore trace
  const isAgentCore = trace.source === 'agentcore';

  // Check if we have rich data from runtime logs (even for AgentCore mode)
  const hasRichData = !!(trace.system_prompt || trace.user_prompt || (trace.messages && trace.messages.length > 0));

  // Parse user prompt for AgentCore traces
  const parsedUserPrompt = useMemo(() => {
    if (!trace.user_prompt) return '';
    if (isAgentCore) {
      return parseAgentCoreContent(trace.user_prompt);
    }
    return trace.user_prompt;
  }, [trace.user_prompt, isAgentCore]);

  // Format messages for display
  const formattedMessages = useMemo(() => {
    if (!trace.messages || trace.messages.length === 0) return '';
    if (isAgentCore) {
      // Format each message individually for AgentCore traces
      return trace.messages
        .map((msg, i) => {
          const role = msg.role.toUpperCase();
          const content = formatMessageContent(msg);
          return `--- ${role} (${i + 1}) ---\n${content}`;
        })
        .join('\n\n');
    }
    // For local traces, show as formatted JSON
    return JSON.stringify(trace.messages, null, 2);
  }, [trace.messages, isAgentCore]);

  const toggleSection = (section: string) => {
    const newExpanded = new Set(expandedSections);
    if (newExpanded.has(section)) {
      newExpanded.delete(section);
    } else {
      newExpanded.add(section);
    }
    setExpandedSections(newExpanded);
  };

  const handleCopy = async (content: string, section: string) => {
    await navigator.clipboard.writeText(content);
    setCopiedSection(section);
    setTimeout(() => setCopiedSection(null), 2000);
  };

  const openModal = (title: string, content: string) => {
    setModalContent({ title, content });
  };

  const closeModal = () => {
    setModalContent(null);
  };

  return (
    <div className="space-y-4">
      {/* Modal for full prompt view */}
      {modalContent && (
        <PromptModal
          title={modalContent.title}
          content={modalContent.content}
          onClose={closeModal}
          onCopy={() => handleCopy(modalContent.content, 'modal')}
          isCopied={copiedSection === 'modal'}
        />
      )}

      {/* Timing Summary */}
      <Card title="Execution Timing" subtitle="Performance breakdown">
        <div className="grid grid-cols-3 gap-4">
          <div className="p-4 bg-[--color-bg-tertiary] rounded-lg">
            <div className="flex items-center gap-2 text-sm text-[--color-text-muted] mb-1">
              <Clock className="w-4 h-4" />
              Total Time
            </div>
            <p className="text-2xl font-semibold text-[--color-accent-primary]">
              {formatMs(trace.timing.total_ms)}
            </p>
          </div>
          <div className="p-4 bg-[--color-bg-tertiary] rounded-lg">
            <div className="flex items-center gap-2 text-sm text-[--color-text-muted] mb-1">
              <Wrench className="w-4 h-4" />
              Tool Time
            </div>
            <p className="text-2xl font-semibold text-[--color-accent-secondary]">
              {formatMs(trace.timing.tool_ms)}
            </p>
          </div>
          <div className="p-4 bg-[--color-bg-tertiary] rounded-lg">
            <div className="flex items-center gap-2 text-sm text-[--color-text-muted] mb-1">
              <MessageSquare className="w-4 h-4" />
              Model Time
            </div>
            <p className="text-2xl font-semibold text-[--color-info]">
              {formatMs(trace.timing.model_ms)}
            </p>
          </div>
        </div>
      </Card>

      {/* Tool Calls */}
      <Card
        title="Tool Calls"
        subtitle={`${trace.tool_calls.length} tool invocations`}
        action={
          <Badge variant={trace.tool_calls.every((t) => t.success) ? 'success' : 'warning'}>
            {trace.tool_calls.filter((t) => t.success).length}/{trace.tool_calls.length} succeeded
          </Badge>
        }
      >
        {trace.tool_calls.length === 0 ? (
          <p className="text-sm text-[--color-text-muted] py-4">No tool calls recorded</p>
        ) : (
          <div className="space-y-3">
            {trace.tool_calls.map((toolCall, index) => (
              <ToolCallItem
                key={index}
                toolCall={toolCall}
                index={index}
                messages={trace.messages}
                isAgentCore={isAgentCore}
                hasRichData={hasRichData}
                toolInputs={trace.tool_inputs}
                toolOutputs={trace.tool_outputs}
              />
            ))}
          </div>
        )}
      </Card>

      {/* System Prompt - only show if available */}
      {trace.system_prompt && (
        <CollapsibleSection
          title="System Prompt"
          icon={<Code className="w-4 h-4" />}
          isExpanded={expandedSections.has('system')}
          onToggle={() => toggleSection('system')}
          onCopy={() => handleCopy(trace.system_prompt, 'system')}
          isCopied={copiedSection === 'system'}
          onExpand={() => openModal('System Prompt', trace.system_prompt)}
        >
          <pre className="text-sm text-[--color-text-secondary] font-mono whitespace-pre-wrap leading-relaxed max-h-[400px] overflow-y-auto">
            {trace.system_prompt}
          </pre>
        </CollapsibleSection>
      )}

      {/* User Prompt - only show if available */}
      {trace.user_prompt && (
        <CollapsibleSection
          title="User Prompt"
          icon={<MessageSquare className="w-4 h-4" />}
          isExpanded={expandedSections.has('user')}
          onToggle={() => toggleSection('user')}
          onCopy={() => handleCopy(parsedUserPrompt, 'user')}
          isCopied={copiedSection === 'user'}
          onExpand={() => openModal('User Prompt', parsedUserPrompt)}
        >
          <pre className="text-sm text-[--color-text-secondary] font-mono whitespace-pre-wrap leading-relaxed max-h-[400px] overflow-y-auto">
            {parsedUserPrompt}
          </pre>
        </CollapsibleSection>
      )}

      {/* Raw Response */}
      {trace.raw_response && (
        <CollapsibleSection
          title="Raw Response"
          icon={<Code className="w-4 h-4" />}
          isExpanded={expandedSections.has('response')}
          onToggle={() => toggleSection('response')}
          onCopy={() => handleCopy(trace.raw_response!, 'response')}
          isCopied={copiedSection === 'response'}
        >
          <pre className="text-sm text-[--color-text-secondary] font-mono whitespace-pre-wrap leading-relaxed max-h-[400px] overflow-y-auto">
            {trace.raw_response}
          </pre>
        </CollapsibleSection>
      )}

      {/* Messages */}
      {trace.messages.length > 0 && (
        <CollapsibleSection
          title="Message History"
          icon={<MessageSquare className="w-4 h-4" />}
          isExpanded={expandedSections.has('messages')}
          onToggle={() => toggleSection('messages')}
          onCopy={() => handleCopy(formattedMessages, 'messages')}
          isCopied={copiedSection === 'messages'}
        >
          <pre className="text-sm text-[--color-text-secondary] font-mono whitespace-pre-wrap leading-relaxed max-h-[400px] overflow-y-auto">
            {formattedMessages}
          </pre>
        </CollapsibleSection>
      )}
    </div>
  );
}

interface ToolCallItemProps {
  toolCall: ToolCallTrace;
  index: number;
  messages: TraceMessage[];
  isAgentCore: boolean;
  hasRichData: boolean;
  toolInputs?: Record<string, unknown>;
  toolOutputs?: Record<string, unknown>;
}

function ToolCallItem({ toolCall, index, messages, isAgentCore, hasRichData, toolInputs, toolOutputs }: ToolCallItemProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  // Check if we have any message data to extract inputs/outputs from
  const hasMessages = messages && messages.length > 0;
  // Check if we have tool_inputs/tool_outputs from AgentCore
  const hasToolInputsOutputs = !!(toolInputs || toolOutputs);

  // Extract tool input from AgentCore tool_inputs using span_id
  const extractAgentCoreInput = useMemo(() => {
    if (!isAgentCore || !toolInputs || !toolCall.span_id) return null;

    const spanData = toolInputs[toolCall.span_id] as { messages?: Array<{ content?: { content?: string } }> } | undefined;
    if (!spanData?.messages?.[0]?.content?.content) return null;

    try {
      // The content is a JSON string like '{"query": "...", "limit": 3}'
      return JSON.parse(spanData.messages[0].content.content);
    } catch {
      // Return raw string if not JSON
      return spanData.messages[0].content.content;
    }
  }, [isAgentCore, toolInputs, toolCall.span_id]);

  // Extract tool output from AgentCore tool_outputs using span_id
  const extractAgentCoreOutput = useMemo(() => {
    if (!isAgentCore || !toolOutputs || !toolCall.span_id) return null;

    const spanData = toolOutputs[toolCall.span_id] as { messages?: Array<{ content?: { message?: string } }> } | undefined;
    if (!spanData?.messages?.[0]?.content?.message) return null;

    try {
      // The message is a JSON string like '[{"text": "..."}]'
      const parsed = JSON.parse(spanData.messages[0].content.message);
      if (Array.isArray(parsed) && parsed.length > 0) {
        // Extract text from the array
        const texts = parsed
          .filter((item: { text?: string }) => item.text)
          .map((item: { text: string }) => item.text);

        if (texts.length > 0) {
          const combinedText = texts.join('\n');

          // For code_interpreter, the text might be a nested Python-style array: [{'type': 'text', 'text': '...'}]
          if (toolCall.tool_name === 'code_interpreter' && combinedText.startsWith('[{')) {
            try {
              // Handle Python-style single quotes by replacing them with double quotes
              const innerParsed = JSON.parse(combinedText.replace(/'/g, '"'));
              if (Array.isArray(innerParsed)) {
                const innerTexts = innerParsed
                  .filter((item: { type?: string; text?: string }) => item.type === 'text' && item.text)
                  .map((item: { text: string }) => item.text);
                if (innerTexts.length > 0) {
                  return innerTexts.join('\n');
                }
              }
            } catch {
              // If inner parsing fails, return the combined text
            }
          }

          return combinedText;
        }
      }
      return null;
    } catch {
      // Return raw string if not JSON
      return spanData.messages[0].content.message;
    }
  }, [isAgentCore, toolOutputs, toolCall.span_id, toolCall.tool_name]);

  // Look up actual input from toolUse in assistant messages
  const toolUseInput = useMemo(() => {
    // For AgentCore mode, try to get from tool_inputs first
    if (isAgentCore && extractAgentCoreInput) {
      return extractAgentCoreInput;
    }
    // For AgentCore mode without tool_inputs or messages, inputs are not captured
    if (isAgentCore && !hasMessages) {
      return null;
    }
    for (const msg of messages) {
      // Ensure msg.content is an array before iterating (it could be a string for some message types)
      if (msg.role === 'assistant' && Array.isArray(msg.content)) {
        for (const content of msg.content) {
          if (content.toolUse?.toolUseId === toolCall.tool_use_id) {
            return content.toolUse.input;
          }
        }
      }
    }
    // Fallback to input_params from tool_calls if messages don't have it
    return toolCall.input_params;
  }, [messages, toolCall.tool_use_id, toolCall.input_params, isAgentCore, hasMessages, extractAgentCoreInput]);

  // Look up actual output from toolResult in user messages
  const toolResultOutput = useMemo(() => {
    // For AgentCore mode, try to get from tool_outputs first
    if (isAgentCore && extractAgentCoreOutput) {
      return extractAgentCoreOutput;
    }
    // For AgentCore mode without tool_outputs or messages, outputs are not captured
    if (isAgentCore && !hasMessages) {
      return null;
    }
    for (const msg of messages) {
      // Ensure msg.content is an array before iterating (it could be a string for some message types)
      if (msg.role === 'user' && Array.isArray(msg.content)) {
        for (const content of msg.content) {
          if (content.toolResult?.toolUseId === toolCall.tool_use_id) {
            // Combine all text content
            const rawText = Array.isArray(content.toolResult.content)
              ? content.toolResult.content.map(c => c.text).join('\n')
              : String(content.toolResult.content || '');

            // Handle code_interpreter output which may be JSON array like [{'type': 'text', 'text': '...'}]
            if (toolCall.tool_name === 'code_interpreter' && rawText.startsWith('[{')) {
              try {
                // Try parsing as JSON (handle Python-style single quotes)
                const parsed = JSON.parse(rawText.replace(/'/g, '"'));
                if (Array.isArray(parsed)) {
                  return parsed
                    .filter((item: { type?: string; text?: string }) => item.type === 'text' && item.text)
                    .map((item: { text: string }) => item.text)
                    .join('\n');
                }
              } catch {
                // If parsing fails, return raw text
              }
            }
            return rawText;
          }
        }
      }
    }
    // Fallback to output from tool_calls
    return toolCall.output;
  }, [messages, toolCall.tool_use_id, toolCall.output, toolCall.tool_name, isAgentCore, hasMessages, extractAgentCoreOutput]);

  // Determine if we should show input/output sections
  // Show for local mode, or AgentCore with rich data, or AgentCore with tool_inputs
  const showInputOutput = !isAgentCore || hasRichData || hasToolInputsOutputs;

  // Format duration nicely
  const formatDuration = (ms: number) => {
    if (ms < 0 || ms > 1000000000) return '—';
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  };

  return (
    <div className="border border-[--color-border] rounded-lg overflow-hidden">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full px-4 py-3 flex items-center justify-between hover:bg-[--color-bg-tertiary]/30 transition-colors"
      >
        <div className="flex items-center gap-3">
          <span className="w-6 h-6 flex items-center justify-center bg-[--color-bg-tertiary] rounded text-xs font-medium text-[--color-text-muted]">
            {index + 1}
          </span>
          <span className="font-mono text-sm text-[--color-accent-primary]">{toolCall.tool_name}</span>
          {toolCall.success ? (
            <CheckCircle className="w-4 h-4 text-[--color-success]" />
          ) : (
            <XCircle className="w-4 h-4 text-[--color-urgent]" />
          )}
          <span className="text-xs text-[--color-text-muted]">{formatDuration(toolCall.duration_ms)}</span>
        </div>
        {isExpanded ? (
          <ChevronDown className="w-4 h-4 text-[--color-text-muted]" />
        ) : (
          <ChevronRight className="w-4 h-4 text-[--color-text-muted]" />
        )}
      </button>

      {isExpanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-[--color-border]">
          {/* AgentCore Mode Notice - only show when rich data is NOT available */}
          {isAgentCore && !hasRichData && (
            <div className="pt-3">
              <p className="text-xs text-[--color-text-muted] italic">
                Input/output data not captured in OTEL trace mode (runtime logs unavailable)
              </p>
            </div>
          )}

          {/* Input - show when we have data to display */}
          {showInputOutput && (
            <div className="pt-3">
              <p className="text-xs text-[--color-text-muted] mb-1">Input</p>
              <pre className="p-3 bg-[--color-bg-tertiary] rounded text-xs font-mono text-[--color-text-secondary] overflow-x-auto max-h-[200px] overflow-y-auto">
                {toolUseInput && Object.keys(toolUseInput).length > 0
                  ? JSON.stringify(toolUseInput, null, 2)
                  : '(no input)'}
              </pre>
            </div>
          )}

          {/* Output - show when we have data to display */}
          {showInputOutput && toolResultOutput && (
            <div>
              <p className="text-xs text-[--color-text-muted] mb-1">Output</p>
              <pre className="p-3 bg-[--color-bg-tertiary] rounded text-xs font-mono text-[--color-text-secondary] overflow-x-auto max-h-[300px] overflow-y-auto whitespace-pre-wrap">
                {toolResultOutput}
              </pre>
            </div>
          )}

          {/* Tool Description - show for AgentCore mode without rich data if available */}
          {isAgentCore && !hasRichData && toolCall.description && (
            <div>
              <p className="text-xs text-[--color-text-muted] mb-1">Tool Description</p>
              <pre className="p-3 bg-[--color-bg-tertiary] rounded text-xs font-mono text-[--color-text-secondary] overflow-x-auto max-h-[150px] overflow-y-auto whitespace-pre-wrap">
                {toolCall.description.split('\n').slice(0, 5).join('\n')}
                {toolCall.description.split('\n').length > 5 && '\n...'}
              </pre>
            </div>
          )}

          {/* Tool Parameters Schema - show for AgentCore mode without rich data if available */}
          {isAgentCore && !hasRichData && toolCall.json_schema && (
            <div>
              <p className="text-xs text-[--color-text-muted] mb-1">Parameters Schema</p>
              <pre className="p-3 bg-[--color-bg-tertiary] rounded text-xs font-mono text-[--color-text-secondary] overflow-x-auto max-h-[200px] overflow-y-auto">
                {(() => {
                  try {
                    const schema = JSON.parse(toolCall.json_schema);
                    // Just show the properties section for brevity
                    if (schema.properties) {
                      return JSON.stringify({ properties: schema.properties, required: schema.required }, null, 2);
                    }
                    return JSON.stringify(schema, null, 2);
                  } catch {
                    return toolCall.json_schema;
                  }
                })()}
              </pre>
            </div>
          )}

          {/* Error */}
          {toolCall.error && (
            <div>
              <p className="text-xs text-[--color-urgent] mb-1">Error</p>
              <pre className="p-3 bg-[--color-urgent]/10 border border-[--color-urgent]/30 rounded text-xs font-mono text-[--color-urgent] overflow-x-auto">
                {toolCall.error}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface CollapsibleSectionProps {
  title: string;
  icon: React.ReactNode;
  isExpanded: boolean;
  onToggle: () => void;
  onCopy: () => void;
  isCopied: boolean;
  onExpand?: () => void;
  children: React.ReactNode;
}

function CollapsibleSection({
  title,
  icon,
  isExpanded,
  onToggle,
  onCopy,
  isCopied,
  onExpand,
  children,
}: CollapsibleSectionProps) {
  return (
    <div className="glass-card overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full px-5 py-4 flex items-center justify-between hover:bg-[--color-bg-tertiary]/30 transition-colors"
      >
        <div className="flex items-center gap-2 text-[--color-text-primary]">
          {icon}
          <span className="font-medium">{title}</span>
        </div>
        {isExpanded ? (
          <ChevronDown className="w-4 h-4 text-[--color-text-muted]" />
        ) : (
          <ChevronRight className="w-4 h-4 text-[--color-text-muted]" />
        )}
      </button>

      {isExpanded && (
        <div className="px-5 pb-5 border-t border-[--color-border]">
          <div className="flex justify-end gap-2 pt-3 mb-2">
            {onExpand && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onExpand();
                }}
                className="flex items-center gap-2 px-3 py-1.5 bg-[--color-bg-tertiary] rounded-lg text-sm text-[--color-text-secondary] hover:text-[--color-text-primary] transition-colors"
              >
                <Maximize2 className="w-4 h-4" />
                Full View
              </button>
            )}
            <button
              onClick={(e) => {
                e.stopPropagation();
                onCopy();
              }}
              className="flex items-center gap-2 px-3 py-1.5 bg-[--color-bg-tertiary] rounded-lg text-sm text-[--color-text-secondary] hover:text-[--color-text-primary] transition-colors"
            >
              {isCopied ? (
                <>
                  <Check className="w-4 h-4 text-[--color-success]" />
                  Copied!
                </>
              ) : (
                <>
                  <Copy className="w-4 h-4" />
                  Copy
                </>
              )}
            </button>
          </div>
          <div className="bg-[--color-bg-tertiary] rounded-lg p-4 overflow-x-auto">{children}</div>
        </div>
      )}
    </div>
  );
}

interface PromptModalProps {
  title: string;
  content: string;
  onClose: () => void;
  onCopy: () => void;
  isCopied: boolean;
}

function PromptModal({ title, content, onClose, onCopy, isCopied }: PromptModalProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal - Light mode only */}
      <div className="relative w-full max-w-5xl max-h-[90vh] bg-white rounded-2xl shadow-2xl border border-slate-200 flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 bg-slate-50 rounded-t-2xl">
          <h2 className="font-display text-xl text-slate-900">{title}</h2>
          <div className="flex items-center gap-2">
            <button
              onClick={onCopy}
              className="flex items-center gap-2 px-3 py-1.5 bg-slate-200 rounded-lg text-sm text-slate-700 hover:bg-slate-300 transition-colors"
            >
              {isCopied ? (
                <>
                  <Check className="w-4 h-4 text-emerald-600" />
                  Copied!
                </>
              ) : (
                <>
                  <Copy className="w-4 h-4" />
                  Copy
                </>
              )}
            </button>
            <button
              onClick={onClose}
              className="p-2 rounded-lg text-slate-500 hover:text-slate-900 hover:bg-slate-200 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 bg-white">
          <pre className="text-sm text-slate-800 font-mono whitespace-pre-wrap leading-relaxed">
            {content}
          </pre>
        </div>
      </div>
    </div>
  );
}

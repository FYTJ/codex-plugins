#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import argparse
import sys
from pathlib import Path
from typing import Any

import codex_background_terminal_patch_app as m

ORIG_APPLY_APP_CONTROL_BRIDGE_PATCH = m.apply_app_control_bridge_patch


def find_text_entry(
    asar_path: Path,
    header: dict[str, Any],
    data_offset: int,
    *,
    step_name: str,
    include_all: tuple[str, ...] = (),
    include_any: tuple[str, ...] = (),
    path_contains: tuple[str, ...] = (),
    path_prefix: str | None = None,
    suffix: str = ".js",
) -> str:
    matches: list[str] = []
    for rel_path, entry in m.iter_asar_entries(header):
        if entry.get("unpacked"):
            continue
        if suffix and not rel_path.endswith(suffix):
            continue
        if path_prefix is not None and not rel_path.startswith(path_prefix):
            continue
        if path_contains and not all(part in rel_path for part in path_contains):
            continue
        text = m.read_asar_file(asar_path, header, data_offset, rel_path).decode("utf-8", "replace")
        if include_all and not all(marker in text for marker in include_all):
            continue
        if include_any and not any(marker in text for marker in include_any):
            continue
        matches.append(rel_path)
    if len(matches) != 1:
        raise m.ControllerError(
            "patch-match-failed",
            f"Expected exactly one ASAR entry for {step_name}.",
            details={
                "step": step_name,
                "matchCount": len(matches),
                "matches": matches[:20],
                "includeAll": list(include_all),
                "includeAny": list(include_any),
                "pathContains": list(path_contains),
                "pathPrefix": path_prefix,
                "suffix": suffix,
            },
        )
    return matches[0]


def action_fn(text: str) -> str:
    if "Br(`clean-background-terminals`" in text or "Br(`list-background-terminals`" in text:
        return "Br"
    if "Ba(`clean-background-terminals`" in text or "Ba(`list-background-terminals`" in text:
        return "Ba"
    if "Xe(`clean-background-terminals`" in text or "Xe(`list-background-terminals`" in text:
        return "Xe"
    if "Bo(`clean-background-terminals`" in text or "Bo(`list-background-terminals`" in text:
        return "Bo"
    return "_n"


def apply_ctrl_b_ui_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> list[dict[str, Any]]:
    manager_clean_before = ")},!1)}getArchiveConversationContext(){"
    manager_after = (
        ")},!1)}async backgroundActiveTerminal(e){let t=this.getStreamRole(e);"
        "if(t?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let n=this.conversations.get(e);"
        f"await this.sendRequest(`{m.CTRL_B_NATIVE_METHOD}`,{{threadId:n?.id??e,source:`user_shortcut`}})"
        "}async listBackgroundTerminals(e,t,n){let r=this.getStreamRole(e);"
        "if(r?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let i=this.conversations.get(e);"
        f"return await this.sendRequest(`{m.LIST_BG_NATIVE_METHOD}`,{{threadId:i?.id??e,cursor:t??null,limit:n??50}})"
        "}async terminateBackgroundTerminal(e,t){let n=this.getStreamRole(e);"
        "if(n?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let r=this.conversations.get(e);"
        f"return await this.sendRequest(`{m.TERMINATE_BG_NATIVE_METHOD}`,{{threadId:r?.id??e,processId:t}})"
        "}getArchiveConversationContext(){"
    )
    manager_ctrl_b_after = (
        ")},!1)}async backgroundActiveTerminal(e){let t=this.getStreamRole(e);"
        "if(t?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let n=this.conversations.get(e);"
        f"await this.sendRequest(`{m.CTRL_B_NATIVE_METHOD}`,{{threadId:n?.id??e,source:`user_shortcut`}})"
        "}getArchiveConversationContext(){"
    )
    manager_ctrl_b_terminate_after = (
        ")},!1)}async backgroundActiveTerminal(e){let t=this.getStreamRole(e);"
        "if(t?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let n=this.conversations.get(e);"
        f"await this.sendRequest(`{m.CTRL_B_NATIVE_METHOD}`,{{threadId:n?.id??e,source:`user_shortcut`}})"
        "}async terminateBackgroundTerminal(e,t){let n=this.getStreamRole(e);"
        "if(n?.role===`follower`)throw Error(`Please continue this conversation on the window where it was started.`);"
        "let r=this.conversations.get(e);"
        f"return await this.sendRequest(`{m.TERMINATE_BG_NATIVE_METHOD}`,{{threadId:r?.id??e,processId:t}})"
        "}getArchiveConversationContext(){"
    )
    manager_rel = find_text_entry(
        asar_path,
        header,
        data_offset,
        step_name="ctrl-b-manager-path",
        include_all=("getArchiveConversationContext",),
        include_any=(manager_clean_before, m.CTRL_B_NATIVE_METHOD),
        path_prefix="webview/assets/",
    )

    command_before_old = (
        '"interrupt-conversation":Q7(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_before_new = (
        '"interrupt-conversation":e9(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_before_current = (
        '"interrupt-conversation":P9(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_before_5059 = (
        '"interrupt-conversation":X7(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_before_5200 = (
        '"interrupt-conversation":F9(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )

    def command_after(before: str, wrapper: str) -> str:
        return (
            before
            + f',"{m.CTRL_B_ACTION}":{wrapper}(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
            + f',"{m.LIST_BG_ACTION}":{wrapper}(async(e,{{conversationId:t,cursor:n,limit:r}})=>{{return await e.listBackgroundTerminals(t,n,r)}})'
            + f',"{m.TERMINATE_BG_ACTION}":{wrapper}(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
        )

    def command_ctrl_b_after(before: str, wrapper: str) -> str:
        return before + f',"{m.CTRL_B_ACTION}":{wrapper}(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'

    def command_ctrl_b_terminate_after(before: str, wrapper: str) -> str:
        return (
            before
            + f',"{m.CTRL_B_ACTION}":{wrapper}(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
            + f',"{m.TERMINATE_BG_ACTION}":{wrapper}(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
        )

    command_rel = find_text_entry(
        asar_path,
        header,
        data_offset,
        step_name="background-terminal-host-command-path",
        include_all=("interrupt-conversation", "markTurnInterruptedByThisClient"),
        path_prefix="webview/assets/",
    )

    keydown_before_old = (
        "(0,DG.useEffect)(()=>{let e=Nl(Un.view,{b:e=>"
        "!(B_()?e.metaKey&&!e.ctrlKey:e.ctrlKey&&!e.metaKey)||e.shiftKey||e.altKey?!1:"
        "(ae(`toggleSidebar`,`composer_sidebar_shortcut`),e.preventDefault(),e.stopPropagation(),!0)});"
        "return()=>{e()}},[Un])"
    )
    keydown_after_old = (
        "(0,DG.useEffect)(()=>{let e=Nl(Un.view,{b:e=>"
        "!(B_()?e.metaKey&&!e.ctrlKey:e.ctrlKey&&!e.metaKey)||e.shiftKey||e.altKey?!1:"
        "(ae(`toggleSidebar`,`composer_sidebar_shortcut`),e.preventDefault(),e.stopPropagation(),!0)}),"
        "t=e=>{e.type===`keydown`&&e.key.toLowerCase()===`b`&&e.ctrlKey===!0&&e.metaKey!==!0&&e.altKey!==!0&&e.shiftKey!==!0&&H?.type===`local`&&"
        f"(e.preventDefault(),e.stopPropagation(),_o(`{m.CTRL_B_ACTION}`,{{conversationId:H.localConversationId}}).catch(e=>{{}}))}};"
        "return window.addEventListener(`keydown`,t,!0),()=>{e(),window.removeEventListener(`keydown`,t,!0)}},[Un,H])"
    )
    keydown_before_new = (
        "(0,Mz.useEffect)(()=>{let e=ci(Y.view,{b:e=>"
        "!(df()?e.metaKey&&!e.ctrlKey:e.ctrlKey&&!e.metaKey)||e.shiftKey||e.altKey?!1:"
        "(cp(`toggleSidebar`,`composer_sidebar_shortcut`),e.preventDefault(),e.stopPropagation(),!0)});"
        "return()=>{e()}},[Y])"
    )
    keydown_after_new = (
        "(0,Mz.useEffect)(()=>{let e=ci(Y.view,{b:e=>"
        "!(df()?e.metaKey&&!e.ctrlKey:e.ctrlKey&&!e.metaKey)||e.shiftKey||e.altKey?!1:"
        "(cp(`toggleSidebar`,`composer_sidebar_shortcut`),e.preventDefault(),e.stopPropagation(),!0)}),"
        "t=e=>{e.type===`keydown`&&e.key.toLowerCase()===`b`&&e.ctrlKey===!0&&e.metaKey!==!0&&e.altKey!==!0&&e.shiftKey!==!0&&ie?.type===`local`&&"
        f"(e.preventDefault(),e.stopPropagation(),pd(`{m.CTRL_B_ACTION}`,{{conversationId:ie.localConversationId}}).catch(e=>{{}}))}};"
        "return window.addEventListener(`keydown`,t,!0),()=>{e(),window.removeEventListener(`keydown`,t,!0)}},[Y,ie])"
    )
    keydown_before_current = "qN(`composer.togglePlanMode`,Se,we);let{serviceTierSettings:Te}=Pm(ae)"
    keydown_after_current = (
        "qN(`composer.togglePlanMode`,Se,we);"
        "(0,Q9.useEffect)(()=>{let e=e=>{e.type===`keydown`&&e.key.toLowerCase()===`b`&&"
        "e.ctrlKey===!0&&e.metaKey!==!0&&e.altKey!==!0&&e.shiftKey!==!0&&ne?.type===`local`&&"
        f"(e.preventDefault(),e.stopPropagation(),pr(`{m.CTRL_B_ACTION}`,{{conversationId:ne.localConversationId}}).catch(e=>{{}}))}};"
        "return window.addEventListener(`keydown`,e,!0),()=>window.removeEventListener(`keydown`,e,!0)},[ne]);"
        "let{serviceTierSettings:Te}=Pm(ae)"
    )
    keydown_before_5059 = "aO(`composer.togglePlanMode`,Ce,Te);let{serviceTierSettings:Ee}=sm(oe)"
    keydown_after_5059 = (
        "aO(`composer.togglePlanMode`,Ce,Te);"
        "(0,w0.useEffect)(()=>{let e=e=>{e.type===`keydown`&&e.key.toLowerCase()===`b`&&"
        "e.ctrlKey===!0&&e.metaKey!==!0&&e.altKey!==!0&&e.shiftKey!==!0&&C?.type===`local`&&"
        f"(e.preventDefault(),e.stopPropagation(),Af(`{m.CTRL_B_ACTION}`,{{conversationId:C.localConversationId}}).catch(e=>{{}}))}};"
        "return window.addEventListener(`keydown`,e,!0),()=>window.removeEventListener(`keydown`,e,!0)},[C]);"
        "let{serviceTierSettings:Ee}=sm(oe)"
    )
    keydown_before_5103 = "jR(`composer.togglePlanMode`,Se,we);let{serviceTierSettings:Te}=Uv(oe)"
    keydown_after_5103 = (
        "jR(`composer.togglePlanMode`,Se,we);"
        "(0,KX.useEffect)(()=>{let e=e=>{e.type===`keydown`&&e.key.toLowerCase()===`b`&&"
        "e.ctrlKey===!0&&e.metaKey!==!0&&e.altKey!==!0&&e.shiftKey!==!0&&"
        "z.value.kind===`local`&&z.value.conversationId!=null&&"
        f"(e.preventDefault(),e.stopPropagation(),to(`{m.CTRL_B_ACTION}`,{{conversationId:z.value.conversationId}}).catch(e=>{{}}))}};"
        "return window.addEventListener(`keydown`,e,!0),()=>window.removeEventListener(`keydown`,e,!0)},[z.value]);"
        "let{serviceTierSettings:Te}=Uv(oe)"
    )
    keydown_before_5200 = "$z(`composer.togglePlanMode`,Se,Ce);let{serviceTierSettings:we}=Qs(oe)"
    keydown_after_5200 = (
        "$z(`composer.togglePlanMode`,Se,Ce);"
        "(0,z$.useEffect)(()=>{let e=e=>{e.type===`keydown`&&e.key.toLowerCase()===`b`&&"
        "e.ctrlKey===!0&&e.metaKey!==!0&&e.altKey!==!0&&e.shiftKey!==!0&&"
        "V.value.kind===`local`&&V.value.conversationId!=null&&"
        f"(e.preventDefault(),e.stopPropagation(),Eu(`{m.CTRL_B_ACTION}`,{{conversationId:V.value.conversationId}}).catch(e=>{{}}))}};"
        "return window.addEventListener(`keydown`,e,!0),()=>window.removeEventListener(`keydown`,e,!0)},[V.value]);"
        "let{serviceTierSettings:we}=Qs(oe)"
    )
    keydown_rel = find_text_entry(
        asar_path,
        header,
        data_offset,
        step_name="ctrl-b-keydown-path",
        include_all=("localConversationId",),
        include_any=(keydown_before_old, keydown_before_new, keydown_before_current, keydown_before_5059, keydown_before_5103, keydown_before_5200, m.CTRL_B_ACTION),
        path_prefix="webview/assets/",
    )

    manager_step = m.replace_asar_text_variants(
        asar_path,
        header,
        data_offset,
        manager_rel,
        [
            (manager_clean_before, manager_after),
            (manager_ctrl_b_after, manager_after),
            (manager_ctrl_b_terminate_after, manager_after),
        ],
        step_name="ctrl-b-conversation-manager-method",
    )
    command_variants = [
        (command_before_old, command_after(command_before_old, "Q7")),
        (command_ctrl_b_after(command_before_old, "Q7"), command_after(command_before_old, "Q7")),
        (command_ctrl_b_terminate_after(command_before_old, "Q7"), command_after(command_before_old, "Q7")),
        (command_before_new, command_after(command_before_new, "e9")),
        (command_ctrl_b_after(command_before_new, "e9"), command_after(command_before_new, "e9")),
        (command_ctrl_b_terminate_after(command_before_new, "e9"), command_after(command_before_new, "e9")),
        (command_before_current, command_after(command_before_current, "P9")),
        (command_ctrl_b_after(command_before_current, "P9"), command_after(command_before_current, "P9")),
        (command_ctrl_b_terminate_after(command_before_current, "P9"), command_after(command_before_current, "P9")),
        (command_before_5059, command_after(command_before_5059, "X7")),
        (command_ctrl_b_after(command_before_5059, "X7"), command_after(command_before_5059, "X7")),
        (command_ctrl_b_terminate_after(command_before_5059, "X7"), command_after(command_before_5059, "X7")),
        (command_before_5200, command_after(command_before_5200, "F9")),
        (command_ctrl_b_after(command_before_5200, "F9"), command_after(command_before_5200, "F9")),
        (command_ctrl_b_terminate_after(command_before_5200, "F9"), command_after(command_before_5200, "F9")),
    ]
    keydown_variants = [
        (keydown_before_old, keydown_after_old),
        (keydown_before_new, keydown_after_new),
        (keydown_before_current, keydown_after_current),
        (keydown_before_5059, keydown_after_5059),
        (keydown_before_5103, keydown_after_5103),
        (keydown_before_5200, keydown_after_5200),
    ]

    if command_rel == keydown_rel:
        original = m.read_asar_file(asar_path, header, data_offset, command_rel)
        text = original.decode("utf-8")
        text, command_step = m.replace_text_variants_in_text(
            text,
            command_rel,
            command_variants,
            step_name="ctrl-b-host-command",
        )
        text, keydown_step = m.replace_text_variants_in_text(
            text,
            keydown_rel,
            keydown_variants,
            step_name="ctrl-b-global-keydown",
        )
        updated = text.encode("utf-8")
        combined_step = {
            "name": "ctrl-b-host-command-and-global-keydown",
            "target": command_rel,
            "beforeSha256": hashlib.sha256(original).hexdigest(),
            "afterSha256": hashlib.sha256(updated).hexdigest(),
            "beforeSize": len(original),
            "afterSize": len(updated),
            "alreadyApplied": command_step["alreadyApplied"] and keydown_step["alreadyApplied"],
            "substeps": [command_step, keydown_step],
            "content": updated,
        }
        return [manager_step, combined_step]

    command_step = m.replace_asar_text_variants(
        asar_path,
        header,
        data_offset,
        command_rel,
        command_variants,
        step_name="ctrl-b-host-command",
    )
    keydown_step = m.replace_asar_text_variants(
        asar_path,
        header,
        data_offset,
        keydown_rel,
        keydown_variants,
        step_name="ctrl-b-global-keydown",
    )
    return [manager_step, command_step, keydown_step]


def apply_task005_ui_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> list[dict[str, Any]]:
    local_thread_rel = find_text_entry(
        asar_path,
        header,
        data_offset,
        step_name="local-conversation-thread-path",
        path_contains=("local-conversation-thread",),
        path_prefix="webview/assets/",
    )
    original = m.read_asar_file(asar_path, header, data_offset, local_thread_rel)
    text = original.decode("utf-8")
    call = action_fn(text)

    status_before = "function Sp(e,t,n){return t==null?!n||e.metrics!=null?`running`:`not-found`:t.status}"
    status_after = (
        "function Sp(e,t,n){return t==null?!n||e.metrics!=null||e.process.source===`background-terminal`"
        "?`running`:`not-found`:t.status}"
    )
    status_current_before = "function Oh(e,t,n){return t==null?!n||e.metrics!=null?`running`:`not-found`:t.status}"
    status_current_after = (
        "function Oh(e,t,n){return t==null?!n||e.metrics!=null||e.process.source===`background-terminal`"
        "?`running`:`not-found`:t.status}"
    )
    status_5200_before = "function kh(e,t,n){return t==null?!n||e.metrics!=null?`running`:`not-found`:t.status}"
    status_5200_after = (
        "function kh(e,t,n){return t==null?!n||e.metrics!=null||e.process.source===`background-terminal`"
        "?`running`:`not-found`:t.status}"
    )
    missing_pid_before = "m=!f&&!p&&o.metrics?.pid==null,h=o.process.cwd!=null&&!u&&!d&&!m"
    missing_pid_after = (
        "m=!f&&!p&&o.metrics?.pid==null&&o.process.source!==`background-terminal`,"
        "h=o.process.cwd!=null&&!u&&!d&&!m"
    )
    summary_before = (
        "let f=d,p;t[5]!==l||t[6]!==u||t[7]!==c||t[8]!==s.id||t[9]!==n||t[10]!==i?"
    )
    summary_after = (
        "let f=d,[Bt,BtSet]=(0,By.useState)([]);"
        "(0,By.useEffect)(()=>{if(!n||i==null){BtSet([]);return}let e=!1,t=async()=>{"
        "try{let r=await "
        f"{call}(`{m.LIST_BG_ACTION}`,{{conversationId:i,cursor:null,limit:50}})"
        ";if(e)return;let a=Array.isArray(r?.data)?r.data:[];"
        "BtSet(a.map(e=>({id:String(e.itemId??e.id??e.processId??`${i}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))}catch{e||BtSet([])}};"
        "t();let r=setInterval(t,1e3);return()=>{e=!0,clearInterval(r)}},[n,i]);"
        "Bt.length>0&&(f=[...Bt,...f.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
        "let p;t[5]!==l||t[6]!==u||t[7]!==c||t[8]!==s.id||t[9]!==n||t[10]!==i?"
    )
    summary_after_old_call = summary_after.replace(f"{call}(`{m.LIST_BG_ACTION}`", f"_n(`{m.LIST_BG_ACTION}`")
    summary_after_new_call = summary_after.replace(f"{call}(`{m.LIST_BG_ACTION}`", f"Bo(`{m.LIST_BG_ACTION}`")
    summary_after_shadowed_state = summary_after.replace("[Bt,BtSet]", "[Bt,St]").replace("BtSet(", "St(")
    summary_after_shadowed_state_old_call = summary_after_shadowed_state.replace(
        f"{call}(`{m.LIST_BG_ACTION}`", f"_n(`{m.LIST_BG_ACTION}`"
    )
    summary_after_shadowed_state_new_call = summary_after_shadowed_state.replace(
        f"{call}(`{m.LIST_BG_ACTION}`", f"Bo(`{m.LIST_BG_ACTION}`"
    )
    summary_mapping_fields = "source:String(e.source??``),status:`running`,"
    summary_current_before = "g=un(`chat-process-register`),_;bb0:{if(i==null){"
    summary_current_after = (
        "g=un(`chat-process-register`),[Bt,BtSet]=(0,Ph.useState)([]),_;"
        "(0,Ph.useEffect)(()=>{if(i==null){BtSet([]);return}let e=!1,t=async()=>{try{let r=await "
        f"{call}(`{m.LIST_BG_ACTION}`,{{conversationId:i,cursor:null,limit:50}});"
        "if(e)return;let a=Array.isArray(r?.data)?r.data:[];"
        "BtSet(a.map(e=>({id:String(e.itemId??e.id??e.processId??`${i}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))}"
        "catch{e||BtSet([])}};t();let r=setInterval(t,1e3);return()=>{e=!0,clearInterval(r)}},[i]);"
        "Bt.length>0&&(n=[...Bt,...n.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
        "bb0:{if(i==null){"
    )
    summary_5059_before = "let g=h,_;t[2]!==s||t[3]!==a||t[4]!==c||t[5]!==o||t[6]!==l?"
    summary_5059_after = (
        "let g=h,[Bt,BtSet]=(0,Ph.useState)([]),_;"
        "(0,Ph.useEffect)(()=>{if(a==null){BtSet([]);return}let e=!1,t=async()=>{try{let r=await "
        f"{call}(`{m.LIST_BG_ACTION}`,{{conversationId:a,cursor:null,limit:50}});"
        "if(e)return;let n=Array.isArray(r?.data)?r.data:[];"
        "BtSet(n.map(e=>({id:String(e.itemId??e.id??e.processId??`${a}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))}"
        "catch{e||BtSet([])}};t();let r=setInterval(t,1e3);return()=>{e=!0,clearInterval(r)}},[a]);"
        "Bt.length>0&&(g=[...Bt,...g.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
        "t[2]!==s||t[3]!==a||t[4]!==c||t[5]!==o||t[6]!==l?"
    )
    summary_5200_before = "let v=_,y;t[2]!==c||t[3]!==o||t[4]!==l||t[5]!==s||t[6]!==u?"
    summary_5200_after = (
        "let v=_,[Bt,BtSet]=(0,Fh.useState)([]);"
        "(0,Fh.useEffect)(()=>{if(o==null){BtSet([]);return}let e=!1,t=async()=>{try{let r=await "
        f"{call}(`{m.LIST_BG_ACTION}`,{{conversationId:o,cursor:null,limit:50}});"
        "if(e)return;let a=Array.isArray(r?.data)?r.data:[];"
        "BtSet(a.map(e=>({id:String(e.itemId??e.id??e.processId??`${o}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))}"
        "catch{e||BtSet([])}};t();let r=setInterval(t,1e3);return()=>{e=!0,clearInterval(r)}},[o]);"
        "Bt.length>0&&(v=[...Bt,...v.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
        "let y;t[2]!==c||t[3]!==o||t[4]!==l||t[5]!==s||t[6]!==u?"
    )
    preserve_command_current_before = (
        "BtSet(a.map(e=>({id:String(e.itemId??e.id??e.processId??`${i}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))"
    )
    preserve_command_current_after = (
        "BtSet(t=>{let r=new Map(t.map(e=>[e.id,e.command]));return a.map(e=>{"
        "let t=String(e.itemId??e.id??e.processId??`${i}:${e.command??``}`),"
        "c=String(e.command??``).trim()||r.get(t)||``;"
        "return{id:t,command:c,cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null}})})"
    )
    preserve_command_5059_before = (
        "BtSet(n.map(e=>({id:String(e.itemId??e.id??e.processId??`${a}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))"
    )
    preserve_command_5059_after = (
        "BtSet(t=>{let r=new Map(t.map(e=>[e.id,e.command]));return n.map(e=>{"
        "let t=String(e.itemId??e.id??e.processId??`${a}:${e.command??``}`),"
        "c=String(e.command??``).trim()||r.get(t)||``;"
        "return{id:t,command:c,cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null}})})"
    )
    preserve_command_5200_before = (
        "BtSet(a.map(e=>({id:String(e.itemId??e.id??e.processId??`${o}:${e.command??``}`),"
        "command:String(e.command??``),cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null})))"
    )
    preserve_command_5200_after = (
        "BtSet(t=>{let r=new Map(t.map(e=>[e.id,e.command]));return a.map(e=>{"
        "let t=String(e.itemId??e.id??e.processId??`${o}:${e.command??``}`),"
        "c=String(e.command??``).trim()||r.get(t)||``;"
        "return{id:t,command:c,cwd:e.cwd??null,processId:e.processId??null,"
        "source:String(e.source??``),status:`running`,output:String(e.output??``),"
        "startedAtMs:e.startedAtMs??null,turnId:e.turnId??null}})})"
    )
    anonymous_summary_before = (
        "Bt.length>0&&(f=[...Bt,...f.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
    )
    anonymous_summary_after = (
        anonymous_summary_before
        + "f=f.filter(e=>String(e.command??``).trim().length>0);"
    )
    anonymous_current_before = (
        "Bt.length>0&&(n=[...Bt,...n.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
    )
    anonymous_current_after = (
        anonymous_current_before
        + "n=n.filter(e=>String(e.command??``).trim().length>0);"
    )
    anonymous_5059_before = (
        "Bt.length>0&&(g=[...Bt,...g.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
    )
    anonymous_5059_after = (
        anonymous_5059_before
        + "g=g.filter(e=>String(e.command??``).trim().length>0);"
    )
    anonymous_5200_before = (
        "Bt.length>0&&(v=[...Bt,...v.filter(e=>!Bt.some(t=>t.id===e.id||"
        "e.processId!=null&&t.processId===e.processId||"
        "e.command===t.command&&e.cwd===t.cwd&&e.turnId===t.turnId))]);"
    )
    anonymous_5200_after = anonymous_5200_before + "v=v.filter(e=>String(e.command??``).trim().length>0);"
    anonymous_registered_row_before = "let b=y,x;t[15]!==m||t[16]!==s||t[17]!==b?"
    anonymous_registered_row_after = (
        "let b=y.filter(e=>String(e.terminal.command??``).trim().length>0),x;"
        "t[15]!==m||t[16]!==s||t[17]!==b?"
    )
    anonymous_registered_row_5200_before = "let x=b,C;t[15]!==h||t[16]!==s||t[17]!==x?"
    anonymous_registered_row_5200_after = (
        "let x=b.filter(e=>String(e.terminal.command??``).trim().length>0),C;"
        "t[15]!==h||t[16]!==s||t[17]!==x?"
    )
    summary_5059_stable_after = summary_5059_after.replace(
        preserve_command_5059_before,
        preserve_command_5059_after,
    ).replace(
        anonymous_5059_before,
        anonymous_5059_after,
    )
    legacy_summary_variants = [
        (value.replace(summary_mapping_fields, ""), value)
        for value in (
            summary_after,
            summary_after_old_call,
            summary_after_new_call,
            summary_after_shadowed_state,
            summary_after_shadowed_state_old_call,
            summary_after_shadowed_state_new_call,
            summary_current_after,
            summary_5059_after,
        )
    ]

    stop_old_before = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)},()=>{u(),il(f,e.process.id)}))}"
    )
    stop_old_after = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)},()=>{u(),il(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(p.current){qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "il(f,e.process.id)},()=>{u(),il(f,e.process.id)}))}"
    )
    stop_new_before = (
        "j=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "as(f,e.process.id)},()=>{u(),as(f,e.process.id)}))}"
    )
    stop_new_after = (
        "j=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "as(f,e.process.id)},()=>{u(),as(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(p.current){ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "as(f,e.process.id)},()=>{u(),as(f,e.process.id)}))}"
    )
    stop_current_before = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Ml(f,e.process.id)},()=>{u(),Ml(f,e.process.id)}))}"
    )
    stop_current_after = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Ml(f,e.process.id)},()=>{u(),Ml(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(p.current){tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Ml(f,e.process.id)},()=>{u(),Ml(f,e.process.id)}))}"
    )
    stop_5059_before = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Dc(f,e.process.id)},()=>{u(),Dc(f,e.process.id)}))}"
    )
    stop_5059_after = (
        "k=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Dc(f,e.process.id)},()=>{u(),Dc(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(p.current){Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Dc(f,e.process.id)},()=>{u(),Dc(f,e.process.id)}))}"
    )
    stop_5059_native_first_after = (
        "k=(e,t)=>{let n=e.metrics?.pid;"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null?"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(n=>{{"
        "if(n?.terminated===!1||n?.data?.terminated===!1){u();Dc(f,e.process.id);return}"
        "if(p.current){Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Dc(f,e.process.id)},()=>{u(),Dc(f,e.process.id)})):n!=null&&"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Dc(f,e.process.id)},()=>{u(),Dc(f,e.process.id)}))}"
    )
    stop_5103_before = (
        "A=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "hs(f,e.process.id)},()=>{u(),hs(f,e.process.id)}))}"
    )
    stop_5103_after = (
        "A=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "hs(f,e.process.id)},()=>{u(),hs(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(p.current){js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "hs(f,e.process.id)},()=>{u(),hs(f,e.process.id)}))}"
    )
    stop_5103_native_first_after = (
        "A=(e,t)=>{let n=e.metrics?.pid;"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null?"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(n=>{{"
        "if(n?.terminated===!1||n?.data?.terminated===!1){u();hs(f,e.process.id);return}"
        "if(p.current){js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "hs(f,e.process.id)},()=>{u(),hs(f,e.process.id)})):n!=null&&"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(p.current){js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "hs(f,e.process.id)},()=>{u(),hs(f,e.process.id)}))}"
    )
    stop_5200_before = (
        "M=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "g.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(m.current){Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Td(p,e.process.id)},()=>{u(),Td(p,e.process.id)}))}"
    )
    stop_5200_after = (
        "M=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "g.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(m.current){Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Td(p,e.process.id)},()=>{u(),Td(p,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "if(m.current){Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Td(p,e.process.id)},()=>{u(),Td(p,e.process.id)}))}"
    )
    stop_5200_native_first_after = (
        "M=(e,t)=>{let n=e.metrics?.pid;"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null?"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(n=>{{"
        "if(n?.terminated===!1||n?.data?.terminated===!1){u();Td(p,e.process.id);return}"
        "if(m.current){Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Td(p,e.process.id)},()=>{u(),Td(p,e.process.id)})):n!=null&&"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "g.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "if(m.current){Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopped`});return}"
        "Td(p,e.process.id)},()=>{u(),Td(p,e.process.id)}))}"
    )

    restart_old_before = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),il(f,e.process.id)}))}"
    )
    restart_old_after = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),il(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(qc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "M(e,t)},()=>{l(),il(f,e.process.id)}))}"
    )
    restart_new_before = (
        "F=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "P(e,t)},()=>{l(),as(f,e.process.id)}))}"
    )
    restart_new_after = (
        "F=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "P(e,t)},()=>{l(),as(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(ws(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "P(e,t)},()=>{l(),as(f,e.process.id)}))}"
    )
    restart_current_before = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),Ml(f,e.process.id)}))}"
    )
    restart_current_after = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),Ml(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(tu(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "M(e,t)},()=>{l(),Ml(f,e.process.id)}))}"
    )
    restart_5059_before = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),Dc(f,e.process.id)}))}"
    )
    restart_5059_after = (
        "N=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),Dc(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "M(e,t)},()=>{l(),Dc(f,e.process.id)}))}"
    )
    restart_5059_native_first_after = (
        "N=(e,t)=>{let n=e.metrics?.pid;"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null?"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(n=>{{"
        "if(n?.terminated===!1||n?.data?.terminated===!1){l();Dc(f,e.process.id);return}"
        "M(e,t)},()=>{l(),Dc(f,e.process.id)})):n!=null&&"
        "(Zc(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "M(e,t)},()=>{l(),Dc(f,e.process.id)}))}"
    )
    restart_5103_before = (
        "P=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "N(e,t)},()=>{l(),hs(f,e.process.id)}))}"
    )
    restart_5103_after = (
        "P=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "N(e,t)},()=>{l(),hs(f,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "N(e,t)},()=>{l(),hs(f,e.process.id)}))}"
    )
    restart_5103_native_first_after = (
        "P=(e,t)=>{let n=e.metrics?.pid;"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null?"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(n=>{{"
        "if(n?.terminated===!1||n?.data?.terminated===!1){l();hs(f,e.process.id);return}"
        "N(e,t)},()=>{l(),hs(f,e.process.id)})):n!=null&&"
        "(js(f,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "h.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "N(e,t)},()=>{l(),hs(f,e.process.id)}))}"
    )
    restart_5200_before = (
        "I=(e,t)=>{let n=e.metrics?.pid;n!=null&&"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "g.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "F(e,t)},()=>{l(),Td(p,e.process.id)}))}"
    )
    restart_5200_after = (
        "I=(e,t)=>{let n=e.metrics?.pid;n!=null?"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "g.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "F(e,t)},()=>{l(),Td(p,e.process.id)})):"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null&&"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(()=>{{"
        "F(e,t)},()=>{l(),Td(p,e.process.id)}))}"
    )
    restart_5200_native_first_after = (
        "I=(e,t)=>{let n=e.metrics?.pid;"
        "e.process.source===`background-terminal`&&e.terminal.processId!=null?"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}}).then(n=>{{"
        "if(n?.terminated===!1||n?.data?.terminated===!1){l();Td(p,e.process.id);return}"
        "F(e,t)},()=>{l(),Td(p,e.process.id)})):n!=null&&"
        "(Hd(p,e.process.id,{row:e,rowIndex:t,sortRow:e,status:`stopping`}),"
        "g.mutateAsync({pid:n}).then(n=>{let{killed:r}=n;"
        "if(!r)throw Error(`Process is no longer running`);"
        "F(e,t)},()=>{l(),Td(p,e.process.id)}))}"
    )

    summary_stop_all_before = (
        f"h=e=>{{a==null||d!=null||(f(e.id),{call}(`clean-background-terminals`,"
        "{conversationId:a}).catch(l).finally(()=>f(null)))}"
    )
    summary_stop_single_after = (
        "h=e=>{a==null||d!=null||e.processId==null||(f(e.id),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:a,processId:e.processId}}).then(e=>{{"
        "if(e?.terminated===!1||e?.data?.terminated===!1)throw Error(`Process is no longer running`)"
        "}).catch(l).finally(()=>f(null)))}"
    )
    summary_stop_all_5103_before = (
        f"m=e=>{{i==null||u!=null||(d(e.id),{call}(`clean-background-terminals`,"
        "{conversationId:i}).catch(c).finally(()=>d(null)))}"
    )
    summary_stop_single_5103_after = (
        "m=e=>{i==null||u!=null||e.processId==null||(d(e.id),"
        f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.processId}}).then(e=>{{"
        "if(e?.terminated===!1||e?.data?.terminated===!1)throw Error(`Process is no longer running`)"
        "}).catch(c).finally(()=>d(null)))}"
    )
    summary_stop_label_before = (
        "id:`codex.localConversation.backgroundTerminals.stop`,"
        "defaultMessage:`Stop all background terminals`"
    )
    summary_stop_label_after = (
        "id:`codex.localConversation.backgroundTerminals.stop`,"
        "defaultMessage:`Stop background terminal`"
    )

    stop_disabled_before = "let O=o.metrics?.pid==null||u||d||f,k;"
    stop_disabled_after = "let O=o.metrics?.pid==null&&o.process.source!==`background-terminal`||u||d||f,k;"
    stop_tooltip_before = "k=o.metrics?.pid==null?(0,kp.jsx)(Y,{...jp.stopMissingProcessTooltip}):void 0"
    stop_tooltip_after = (
        "k=o.metrics?.pid==null&&o.process.source!==`background-terminal`"
        "?(0,kp.jsx)(Y,{...jp.stopMissingProcessTooltip}):void 0"
    )
    stop_interactive_before = "let A=o.metrics?.pid==null,j;"
    stop_interactive_after = "let A=o.metrics?.pid==null&&o.process.source!==`background-terminal`,j;"
    stop_disabled_current_before = "let D=o.metrics?.pid==null||u||d||f,O;"
    stop_disabled_current_after = (
        "let D=o.metrics?.pid==null&&o.process.source!==`background-terminal`||u||d||f,O;"
    )
    stop_tooltip_current_before = (
        "O=o.metrics?.pid==null?(0,Fh.jsx)(J,{...Lh.stopMissingProcessTooltip}):void 0"
    )
    stop_tooltip_current_after = (
        "O=o.metrics?.pid==null&&o.process.source!==`background-terminal`"
        "?(0,Fh.jsx)(J,{...Lh.stopMissingProcessTooltip}):void 0"
    )
    stop_tooltip_5059_before = "O=o.metrics?.pid==null?(0,Fh.jsx)(H,{...Lh.stopMissingProcessTooltip}):void 0"
    stop_tooltip_5059_after = (
        "O=o.metrics?.pid==null&&o.process.source!==`background-terminal`"
        "?(0,Fh.jsx)(H,{...Lh.stopMissingProcessTooltip}):void 0"
    )
    stop_interactive_current_before = "let k=o.metrics?.pid==null,A;"
    stop_interactive_current_after = "let k=o.metrics?.pid==null&&o.process.source!==`background-terminal`,A;"
    stop_disabled_5103_before = "let k=o.metrics?.pid==null||u||d||f,A;"
    stop_disabled_5103_after = "let k=o.metrics?.pid==null&&o.process.source!==`background-terminal`||u||d||f,A;"
    stop_tooltip_5103_before = "A=o.metrics?.pid==null?(0,Fh.jsx)(G,{...Lh.stopMissingProcessTooltip}):void 0"
    stop_tooltip_5103_after = (
        "A=o.metrics?.pid==null&&o.process.source!==`background-terminal`"
        "?(0,Fh.jsx)(G,{...Lh.stopMissingProcessTooltip}):void 0"
    )
    stop_tooltip_5200_before = "O=o.metrics?.pid==null?(0,Ih.jsx)(J,{...Rh.stopMissingProcessTooltip}):void 0"
    stop_tooltip_5200_after = (
        "O=o.metrics?.pid==null&&o.process.source!==`background-terminal`"
        "?(0,Ih.jsx)(J,{...Rh.stopMissingProcessTooltip}):void 0"
    )
    stop_interactive_5103_before = "let j=o.metrics?.pid==null,M;"
    stop_interactive_5103_after = "let j=o.metrics?.pid==null&&o.process.source!==`background-terminal`,M;"

    replacements = [
        (
            "task005-summary-native-terminal-list",
            [
                (summary_before, summary_after),
                (summary_after_old_call, summary_after),
                (summary_after_new_call, summary_after),
                (summary_after_shadowed_state, summary_after),
                (summary_after_shadowed_state_old_call, summary_after),
                (summary_after_shadowed_state_new_call, summary_after),
                (summary_current_before, summary_current_after),
                (summary_5059_before, summary_5059_stable_after),
                (summary_5059_after, summary_5059_stable_after),
                (summary_5200_before, summary_5200_after),
                *legacy_summary_variants,
            ],
        ),
        (
            "task005-preserve-native-terminal-command",
            [
                (preserve_command_current_before, preserve_command_current_after),
                (preserve_command_5059_before, preserve_command_5059_after),
                (preserve_command_5200_before, preserve_command_5200_after),
            ],
        ),
        (
            "task005-drop-anonymous-terminal-summary-rows",
            [
                (anonymous_summary_before, anonymous_summary_after),
                (anonymous_current_before, anonymous_current_after),
                (anonymous_5059_before, anonymous_5059_after),
                (anonymous_5200_before, anonymous_5200_after),
            ],
        ),
        (
            "task005-drop-anonymous-terminal-registered-rows",
            [(anonymous_registered_row_before, anonymous_registered_row_after), (anonymous_registered_row_5200_before, anonymous_registered_row_5200_after)],
        ),
        ("task005-native-terminal-status-running", [(status_before, status_after), (status_current_before, status_current_after), (status_5200_before, status_5200_after)]),
        ("task005-native-terminal-restart-enabled", [(missing_pid_before, missing_pid_after)]),
        (
            "task005-native-terminal-stop-action",
            [
                (stop_old_before, stop_old_after),
                (stop_new_before, stop_new_after),
                (stop_current_before, stop_current_after),
                (stop_5059_before, stop_5059_after),
                (stop_5059_before, stop_5059_native_first_after),
                (stop_5103_before, stop_5103_after),
                (stop_5200_before, stop_5200_after),
            ],
        ),
        (
            "task005-native-terminal-stop-prioritized",
            [(stop_5059_after, stop_5059_native_first_after), (stop_5103_after, stop_5103_native_first_after), (stop_5200_after, stop_5200_native_first_after)],
        ),
        (
            "task005-native-terminal-restart-action",
            [
                (restart_old_before, restart_old_after),
                (restart_new_before, restart_new_after),
                (restart_current_before, restart_current_after),
                (restart_5059_before, restart_5059_after),
                (restart_5059_before, restart_5059_native_first_after),
                (restart_5103_before, restart_5103_after),
                (restart_5200_before, restart_5200_after),
            ],
        ),
        (
            "task005-native-terminal-restart-prioritized",
            [(restart_5059_after, restart_5059_native_first_after), (restart_5103_after, restart_5103_native_first_after), (restart_5200_after, restart_5200_native_first_after)],
        ),
        (
            "task005-summary-stop-single-native-terminal",
            [(summary_stop_all_before, summary_stop_single_after), (summary_stop_all_5103_before, summary_stop_single_5103_after)],
        ),
        (
            "task005-summary-stop-single-label",
            [(summary_stop_label_before, summary_stop_label_after)],
        ),
        ("task005-native-terminal-stop-enabled", [(stop_disabled_before, stop_disabled_after), (stop_disabled_current_before, stop_disabled_current_after), (stop_disabled_5103_before, stop_disabled_5103_after)]),
        ("task005-native-terminal-stop-tooltip", [(stop_tooltip_before, stop_tooltip_after), (stop_tooltip_current_before, stop_tooltip_current_after), (stop_tooltip_5059_before, stop_tooltip_5059_after), (stop_tooltip_5103_before, stop_tooltip_5103_after), (stop_tooltip_5200_before, stop_tooltip_5200_after)]),
        ("task005-native-terminal-stop-tooltip-interactive", [(stop_interactive_before, stop_interactive_after), (stop_interactive_current_before, stop_interactive_current_after), (stop_interactive_5103_before, stop_interactive_5103_after)]),
    ]
    substeps = []
    for step_name, variants in replacements:
        text, substep = m.replace_text_variants_in_text(text, local_thread_rel, variants, step_name=step_name)
        substeps.append(substep)

    updated = text.encode("utf-8")
    syntax_check = m.javascript_syntax_check(local_thread_rel, text)
    if syntax_check.get("ok") is not True:
        raise m.ControllerError("javascript-syntax-check-failed", "Patched local conversation thread bundle failed JavaScript syntax validation.", details=syntax_check)
    return [{
        "name": "task005-native-terminal-controls",
        "target": local_thread_rel,
        "beforeSha256": hashlib.sha256(original).hexdigest(),
        "afterSha256": hashlib.sha256(updated).hexdigest(),
        "beforeSize": len(original),
        "afterSize": len(updated),
        "alreadyApplied": all(step["alreadyApplied"] for step in substeps),
        "substeps": substeps,
        "syntaxCheck": syntax_check,
        "action": m.TERMINATE_BG_ACTION,
        "method": m.TERMINATE_BG_NATIVE_METHOD,
        "content": updated,
    }]


def apply_app_control_bridge_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> dict[str, Any]:
    rel_path = find_text_entry(
        asar_path,
        header,
        data_offset,
        step_name="app-control-main-path",
        include_all=("Failed to warm recommended skills cache", "appServerConnectionRegistry"),
        path_prefix=".vite/build/main-",
    )
    old = m.APP_CONTROL_MAIN_REL
    m.APP_CONTROL_MAIN_REL = rel_path
    try:
        return ORIG_APPLY_APP_CONTROL_BRIDGE_PATCH(asar_path, header, data_offset)
    finally:
        m.APP_CONTROL_MAIN_REL = old


def apply_output_tab_command_header_patch(asar_path: Path, header: dict[str, Any], data_offset: int) -> dict[str, Any]:
    rel_path = find_text_entry(
        asar_path,
        header,
        data_offset,
        step_name="output-tab-path",
        include_all=("backgroundTerminalTab.noOutput", "background-terminal:${n}:${t.id}"),
        path_prefix="webview/assets/",
    )
    original = m.read_asar_file(asar_path, header, data_offset, rel_path)
    text = original.decode("utf-8")

    function_old_before = (
        "function Oce(e){let t=(0,Y6.c)(5),{conversationId:n,terminalId:r}=e,i=Os(Yc,n),a;"
        "t[0]!==r||t[1]!==i?(a=jce(i,r),t[0]=r,t[1]=i,t[2]=a):a=t[2];"
        "let o=a,s=kce(r),c=o?.aggregatedOutput??s?.buffer??``,l;"
        "return t[3]===c?l=t[4]:(l=(0,Z6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:c.length>0?(0,Z6.jsx)(Ece,{output:c}):(0,Z6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Z6.jsx)(H,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=c,t[4]=l),l}"
    )
    function_old_after = (
        "function Oce(e){let t=(0,Y6.c)(5),{conversationId:n,terminalId:r,command:i,output:a}=e,o=Os(Yc,n),s;"
        "t[0]!==r||t[1]!==o?(s=jce(o,r),t[0]=r,t[1]=o,t[2]=s):s=t[2];"
        "let c=s,l=kce(r),u=c?.aggregatedOutput??l?.buffer??a??``,d=c==null?i??``:Nb(c);"
        "d.length===0&&(d=i??``);let f=d.length>0?`${d}\\n${u}`:u,h;"
        "return t[3]===f?h=t[4]:(h=(0,Z6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:f.length>0?(0,Z6.jsx)(Ece,{output:f}):(0,Z6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Z6.jsx)(H,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=f,t[4]=h),h}"
    )
    function_new_before = (
        "function ule(e){let t=(0,H6.c)(5),{conversationId:n,terminalId:r}=e,i=St(ch,n),a;"
        "t[0]!==r||t[1]!==i?(a=ple(i,r),t[0]=r,t[1]=i,t[2]=a):a=t[2];"
        "let o=a,s=dle(r),c=o?.aggregatedOutput??s?.buffer??``,l;"
        "return t[3]===c?l=t[4]:(l=(0,W6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:c.length>0?(0,W6.jsx)(cle,{output:c}):(0,W6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,W6.jsx)(X,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=c,t[4]=l),l}"
    )
    function_new_after = (
        "function ule(e){let t=(0,H6.c)(5),{conversationId:n,terminalId:r,command:i,output:a}=e,o=St(ch,n),s;"
        "t[0]!==r||t[1]!==o?(s=ple(o,r),t[0]=r,t[1]=o,t[2]=s):s=t[2];"
        "let c=s,l=dle(r),u=c?.aggregatedOutput??l?.buffer??a??``,d=c==null?i??``:yo(c);"
        "d.length===0&&(d=i??``);let f=d.length>0?`${d}\\n${u}`:u,h;"
        "return t[3]===f?h=t[4]:(h=(0,W6.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:f.length>0?(0,W6.jsx)(cle,{output:f}):(0,W6.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,W6.jsx)(X,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=f,t[4]=h),h}"
    )
    function_current_before = (
        "function Pi(e){let t=(0,Ri.c)(5),{conversationId:n,terminalId:r}=e,i=ge(le,n),a;"
        "t[0]!==r||t[1]!==i?(a=Li(i,r),t[0]=r,t[1]=i,t[2]=a):a=t[2];"
        "let o=a,s=Fi(r),c=o?.aggregatedOutput??s?.buffer??``,l;"
        "return t[3]===c?l=t[4]:(l=(0,Bi.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:c.length>0?(0,Bi.jsx)(Di,{output:c}):(0,Bi.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Bi.jsx)(we,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=c,t[4]=l),l}"
    )
    function_current_after = (
        "function Pi(e){let t=(0,Ri.c)(5),{conversationId:n,terminalId:r,command:i,output:a}=e,o=ge(le,n),s;"
        "t[0]!==r||t[1]!==o?(s=Li(o,r),t[0]=r,t[1]=o,t[2]=s):s=t[2];"
        "let c=s,l=Fi(r),u=c?.aggregatedOutput??l?.buffer??a??``,d=i??``,f=d.length>0?`${d}\\n${u}`:u,h;"
        "return t[3]===f?h=t[4]:(h=(0,Bi.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:f.length>0?(0,Bi.jsx)(Di,{output:f}):(0,Bi.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,Bi.jsx)(we,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=f,t[4]=h),h}"
    )
    function_5059_before = (
        "function __e(e){let t=(0,v8.c)(5),{conversationId:n,terminalId:r}=e,i=Zr(Hi,n),a;"
        "t[0]!==r||t[1]!==i?(a=b_e(i,r),t[0]=r,t[1]=i,t[2]=a):a=t[2];"
        "let o=a,s=v_e(r),c=o?.aggregatedOutput??s?.buffer??``,l;"
        "return t[3]===c?l=t[4]:(l=(0,b8.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:c.length>0?(0,b8.jsx)(h_e,{output:c}):(0,b8.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,b8.jsx)(W,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=c,t[4]=l),l}"
    )
    function_5059_after = (
        "function __e(e){let t=(0,v8.c)(5),{conversationId:n,terminalId:r,command:i,output:a}=e,o=Zr(Hi,n),s;"
        "t[0]!==r||t[1]!==o?(s=b_e(o,r),t[0]=r,t[1]=o,t[2]=s):s=t[2];"
        "let c=s,l=v_e(r),u=c?.aggregatedOutput??l?.buffer??a??``,d=i??``,f=d.length>0?`${d}\\n${u}`:u,h;"
        "return t[3]===f?h=t[4]:(h=(0,b8.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:f.length>0?(0,b8.jsx)(h_e,{output:f}):(0,b8.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,b8.jsx)(W,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=f,t[4]=h),h}"
    )
    function_5103_before = (
        "function pXe(e){let t=(0,c5.c)(5),{conversationId:n,terminalId:r}=e,i=Qh(ef,n),a;"
        "t[0]!==r||t[1]!==i?(a=gXe(i,r),t[0]=r,t[1]=i,t[2]=a):a=t[2];"
        "let o=a,s=mXe(r),c=o?.aggregatedOutput??s?.buffer??``,l;"
        "return t[3]===c?l=t[4]:(l=(0,u5.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:c.length>0?(0,u5.jsx)(dXe,{output:c}):(0,u5.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,u5.jsx)(X,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=c,t[4]=l),l}"
    )
    function_5103_after = (
        "function pXe(e){let t=(0,c5.c)(5),{conversationId:n,terminalId:r,command:i,output:a}=e,o=Qh(ef,n),s;"
        "t[0]!==r||t[1]!==o?(s=gXe(o,r),t[0]=r,t[1]=o,t[2]=s):s=t[2];"
        "let c=s,l=mXe(r),u=c?.aggregatedOutput??l?.buffer??a??``,d=i??``,f=d.length>0?`${d}\\n${u}`:u,h;"
        "return t[3]===f?h=t[4]:(h=(0,u5.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:f.length>0?(0,u5.jsx)(dXe,{output:f}):(0,u5.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,u5.jsx)(X,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=f,t[4]=h),h}"
    )
    function_5200_before = (
        "function it(e){let t=(0,U.c)(5),{conversationId:n,terminalId:r}=e,i=x(L,n),a;"
        "t[0]!==r||t[1]!==i?(a=st(i,r),t[0]=r,t[1]=i,t[2]=a):a=t[2];"
        "let o=a,s=at(r),c=o?.aggregatedOutput??s?.buffer??``,l;"
        "return t[3]===c?l=t[4]:(l=(0,W.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:c.length>0?(0,W.jsx)(Qe,{output:c}):(0,W.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,W.jsx)(Oe,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=c,t[4]=l),l}"
    )
    function_5200_after = (
        "function it(e){let t=(0,U.c)(5),{conversationId:n,terminalId:r,command:i,output:a}=e,o=x(L,n),s;"
        "t[0]!==r||t[1]!==o?(s=st(o,r),t[0]=r,t[1]=o,t[2]=s):s=t[2];"
        "let c=s,l=at(r),u=c?.aggregatedOutput??l?.buffer??a??``,d=i??``,f=d.length>0?`${d}\\n${u}`:u,h;"
        "return t[3]===f?h=t[4]:(h=(0,W.jsx)(`div`,{className:`h-full min-h-0 bg-token-main-surface-primary`,"
        "children:f.length>0?(0,W.jsx)(Qe,{output:f}):(0,W.jsx)(`div`,{className:`font-vscode-editor text-size-code-sm p-4 text-token-description-foreground`,"
        "children:(0,W.jsx)(Oe,{id:`codex.localConversation.backgroundTerminalTab.noOutput`,defaultMessage:`No output yet`,"
        "description:`Placeholder shown in a background terminal output tab before any terminal output is available`})})}),t[3]=f,t[4]=h),h}"
    )
    props_before = "props:{conversationId:n,terminalId:t.id},id:`background-terminal:${n}:${t.id}`"
    props_command_after = "props:{conversationId:n,terminalId:t.id,command:t.command},id:`background-terminal:${n}:${t.id}`"
    props_after = "props:{conversationId:n,terminalId:t.id,command:t.command,output:t.output??``},id:`background-terminal:${n}:${t.id}`"

    command_before_new = (
        '"interrupt-conversation":e9(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_after_new = (
        command_before_new
        + f',"{m.CTRL_B_ACTION}":e9(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
        + f',"{m.LIST_BG_ACTION}":e9(async(e,{{conversationId:t,cursor:n,limit:r}})=>{{return await e.listBackgroundTerminals(t,n,r)}})'
        + f',"{m.TERMINATE_BG_ACTION}":e9(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
    )
    command_before_current = (
        '"interrupt-conversation":P9(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_after_current = (
        command_before_current
        + f',"{m.CTRL_B_ACTION}":P9(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
        + f',"{m.LIST_BG_ACTION}":P9(async(e,{{conversationId:t,cursor:n,limit:r}})=>{{return await e.listBackgroundTerminals(t,n,r)}})'
        + f',"{m.TERMINATE_BG_ACTION}":P9(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
    )
    command_before_5059 = (
        '"interrupt-conversation":X7(async(e,{conversationId:t,initiatedBy:n},r)=>'
        "{let i=await e.interruptConversation(t);"
        "n===`user`&&i!=null&&r.markTurnInterruptedByThisClient(t,i)})"
    )
    command_after_5059 = (
        command_before_5059
        + f',"{m.CTRL_B_ACTION}":X7(async(e,{{conversationId:t}})=>{{await e.backgroundActiveTerminal(t)}})'
        + f',"{m.LIST_BG_ACTION}":X7(async(e,{{conversationId:t,cursor:n,limit:r}})=>{{return await e.listBackgroundTerminals(t,n,r)}})'
        + f',"{m.TERMINATE_BG_ACTION}":X7(async(e,{{conversationId:t,processId:n}})=>{{return await e.terminateBackgroundTerminal(t,n)}})'
    )

    substeps: list[dict[str, Any]] = []
    text, step = m.replace_text_variants_in_text(
        text,
        rel_path,
        [
            (function_old_before, function_old_after),
            (function_new_before, function_new_after),
            (function_current_before, function_current_after),
            (function_5059_before, function_5059_after),
            (function_5103_before, function_5103_after),
            (function_5200_before, function_5200_after),
        ],
        step_name="output-tab-command-line-header",
    )
    substeps.append(step)
    text, step = m.replace_text_variants_in_text(text, rel_path, [(props_before, props_after), (props_command_after, props_after)], step_name="output-tab-command-prop")
    substeps.append(step)
    command_variants = [
        (command_before_new, command_after_new),
        (command_before_current, command_after_current),
        (command_before_5059, command_after_5059),
    ]
    if any(before in text or after in text for before, after in command_variants):
        text, step = m.replace_text_variants_in_text(
            text,
            rel_path,
            command_variants,
            step_name="output-tab-preserve-background-terminal-host-commands",
        )
        substeps.append(step)
    else:
        substeps.append({
            "name": "output-tab-preserve-background-terminal-host-commands",
            "target": rel_path,
            "alreadyApplied": True,
            "matchedVariant": "registry-not-co-located",
        })

    updated = text.encode("utf-8")
    syntax_check = m.javascript_syntax_check(rel_path, text)
    if syntax_check.get("ok") is not True:
        raise m.ControllerError("javascript-syntax-check-failed", "Patched output tab bundle failed JavaScript syntax validation.", details=syntax_check)
    return {
        "name": "output-tab-command-header",
        "target": rel_path,
        "beforeSha256": hashlib.sha256(original).hexdigest(),
        "afterSha256": hashlib.sha256(updated).hexdigest(),
        "beforeSize": len(original),
        "afterSize": len(updated),
        "alreadyApplied": all(step["alreadyApplied"] for step in substeps),
        "substeps": substeps,
        "syntaxCheck": syntax_check,
        "content": updated,
    }


def scan_task005_ui_bindings(app: Path) -> dict[str, Any]:
    if not app.exists():
        return {"ok": False, "reason": "app-missing", "checks": {}, "matches": {}}
    paths = m.app_paths(app)
    header, _header_size, data_offset = m.read_asar_header(paths["asar"])
    try:
        local_rel = find_text_entry(paths["asar"], header, data_offset, step_name="scan-local-thread-path", path_contains=("local-conversation-thread",), path_prefix="webview/assets/")
        automations_rel = find_text_entry(paths["asar"], header, data_offset, step_name="scan-output-tab-path", include_all=("backgroundTerminalTab.noOutput", "background-terminal:${n}:${t.id}"), path_prefix="webview/assets/")
        local_text = m.read_asar_file(paths["asar"], header, data_offset, local_rel).decode("utf-8", "replace")
        automations_text = m.read_asar_file(paths["asar"], header, data_offset, automations_rel).decode("utf-8", "replace")
    except m.ControllerError as exc:
        return {"ok": False, "reason": exc.reason, "checks": {}, "matches": {}, "details": exc.details}

    call = action_fn(local_text)
    syntax_check = m.javascript_syntax_check(local_rel, local_text)
    automations_syntax_check = m.javascript_syntax_check(automations_rel, automations_text)
    list_method_count = 0
    terminate_method_count = 0
    list_host_registry_present = False
    terminate_host_registry_present = False
    for rel_path, entry in m.iter_asar_entries(header):
        if entry.get("unpacked") or not rel_path.endswith(".js"):
            continue
        text = m.read_asar_file(paths["asar"], header, data_offset, rel_path).decode("utf-8", "replace")
        list_method_count += text.count(m.LIST_BG_NATIVE_METHOD)
        terminate_method_count += text.count(m.TERMINATE_BG_NATIVE_METHOD)
        list_host_registry_present = list_host_registry_present or f'"{m.LIST_BG_ACTION}":' in text
        terminate_host_registry_present = terminate_host_registry_present or f'"{m.TERMINATE_BG_ACTION}":' in text
    list_calls = [
        f"_n(`{m.LIST_BG_ACTION}`,{{conversationId:i,cursor:null,limit:50}})",
        f"Bo(`{m.LIST_BG_ACTION}`,{{conversationId:i,cursor:null,limit:50}})",
        f"Xe(`{m.LIST_BG_ACTION}`,{{conversationId:i,cursor:null,limit:50}})",
        f"Xe(`{m.LIST_BG_ACTION}`,{{conversationId:a,cursor:null,limit:50}})",
        f"Ba(`{m.LIST_BG_ACTION}`,{{conversationId:a,cursor:null,limit:50}})",
        f"Br(`{m.LIST_BG_ACTION}`,{{conversationId:o,cursor:null,limit:50}})",
    ]
    checks = {
        "summaryPollsNativeBackgroundTerminalList": any(call in local_text for call in list_calls),
        "summaryMergesNativeBackgroundTerminalList": (
            "f=[...Bt,...f.filter" in local_text
            or "n=[...Bt,...n.filter" in local_text
            or "g=[...Bt,...g.filter" in local_text
            or "v=[...Bt,...v.filter" in local_text
        ),
        "summaryMapsNativeBackgroundTerminalOutput": "output:String(e.output??``)" in local_text,
        "summaryPreservesLastKnownCommand": "new Map(t.map(e=>[e.id,e.command]))" in local_text,
        "summaryDropsAnonymousTerminalRows": (
            "filter(e=>String(e.command??``).trim().length>0)" in local_text
            and "filter(e=>String(e.terminal.command??``).trim().length>0)" in local_text
        ),
        "summaryUsesCommandTitle": "e.terminal.command.length>0?e.terminal.command" in local_text,
        "outputMenuPresent": "codex.localConversation.backgroundTerminals.openOutput" in local_text and "Open output" in local_text,
        "nativeListHostActionPresent": m.LIST_BG_ACTION in local_text or m.LIST_BG_ACTION in automations_text,
        "nativeListMethodPresent": list_method_count > 0,
        "nativeListHostCommandRegistryPresent": list_host_registry_present and terminate_host_registry_present,
        "nativeTerminateHostActionPresent": m.TERMINATE_BG_ACTION in local_text or m.TERMINATE_BG_ACTION in automations_text,
        "nativeTerminateMethodPresent": terminate_method_count > 0,
        "nativeStatusStaysRunningWithoutMetrics": (
            "e.metrics!=null||e.process.source===`background-terminal`?`running`" in local_text
        ),
        "nativeRowsEnabledWithoutOsPid": "o.metrics?.pid==null&&o.process.source!==`background-terminal`" in local_text,
        "nativeStopUsesProcessId": (
            f"`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.terminal.processId}})"
            in local_text
        ),
        "nativeStopPrioritizesProcessId": (
            "e.process.source===`background-terminal`&&e.terminal.processId!=null?" in local_text
        ),
        "nativeTerminateResultValidated": (
            "?.terminated===!1||" in local_text and "?.data?.terminated===!1" in local_text
        ),
        "summaryStopTargetsSingleNativeTerminal": (
            (
                f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:a,processId:e.processId}})" in local_text
                or f"{call}(`{m.TERMINATE_BG_ACTION}`,{{conversationId:i,processId:e.processId}})" in local_text
            )
            and "defaultMessage:`Stop background terminal`" in local_text
        ),
        "nativeRestartUsesExistingCommandCwdBridge": "runHeadlessAction" in local_text and "{command:n.command,cwd:n.cwd}" in local_text,
        "nativeStopAndRestartBothBound": local_text.count(f"`{m.TERMINATE_BG_ACTION}`") >= 2,
        "localThreadJsSyntaxOk": syntax_check.get("ok") is True,
        "outputTabReceivesCommandAndOutputProps": "props:{conversationId:n,terminalId:t.id,command:t.command,output:t.output??``}" in automations_text,
        "outputTabPrependsCommandLine": (
            "aggregatedOutput??" in automations_text
            and "?.buffer??a??``" in automations_text
            and "`${d}\\n${u}`" in automations_text
        ),
        "outputTabJsSyntaxOk": automations_syntax_check.get("ok") is True,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "matches": {
            "target": local_rel,
            "automationsTarget": automations_rel,
            "localTerminateCallCount": local_text.count(f"`{m.TERMINATE_BG_ACTION}`"),
            "localListCallCount": local_text.count(f"`{m.LIST_BG_ACTION}`"),
            "outputCommandPropCount": automations_text.count("props:{conversationId:n,terminalId:t.id,command:t.command,output:t.output??``}"),
            "listMethodCount": list_method_count,
            "terminateMethodCount": terminate_method_count,
        },
        "syntaxCheck": syntax_check,
        "outputTabSyntaxCheck": automations_syntax_check,
    }


def scan_app_control_bridge(app: Path) -> dict[str, Any]:
    if not app.exists():
        return {"ok": False, "reason": "app-missing", "checks": {}, "matches": {}}
    paths = m.app_paths(app)
    header, _header_size, data_offset = m.read_asar_header(paths["asar"])
    try:
        rel_path = find_text_entry(paths["asar"], header, data_offset, step_name="scan-main-path", include_all=("__cbtAppControl", "appServerConnectionRegistry"), path_prefix=".vite/build/main-")
        main_text = m.read_asar_file(paths["asar"], header, data_offset, rel_path).decode("utf-8", "replace")
    except m.ControllerError as exc:
        return {"ok": False, "reason": exc.reason, "checks": {}, "matches": {}, "details": exc.details}

    syntax_check = m.javascript_syntax_check(rel_path, main_text)
    checks = {
        "markerPresent": m.APP_CONTROL_MARKER in main_text,
        "bridgeFunctionPresent": "__cbtAppControl" in main_text,
        "bridgeStartedFromAppController": "__cbtAppControl(this)" in main_text,
        "startsNativeThreads": "start-ui-thread" in main_text and ".startThread({" in main_text,
        "startsNativeTurns": "start-turn" in main_text and ".startTurn(" in main_text,
        "queriesNativeBackgroundTerminals": "thread/backgroundTerminals/list" in main_text,
        "terminatesNativeBackgroundTerminals": "thread/backgroundTerminals/terminate" in main_text,
        "usesPrimaryWindowRouteNavigation": "navigate-to-route" in main_text and "getPrimaryWindow()" in main_text,
        "rendererDomAutomationBounded": "renderer-click-text" in main_text and "renderer-dom-text" in main_text,
        "rendererRightPanelAutomationBounded": "renderer-right-panel-text" in main_text and "renderer-click-right-panel-text" in main_text,
        "mainJsSyntaxOk": syntax_check.get("ok") is True,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "matches": {"target": rel_path, "markerCount": main_text.count(m.APP_CONTROL_MARKER), "functionCount": main_text.count("__cbtAppControl")},
        "syntaxCheck": syntax_check,
    }


def install_monkeypatches() -> None:
    m.apply_ctrl_b_ui_patch = apply_ctrl_b_ui_patch
    m.apply_task005_ui_patch = apply_task005_ui_patch
    m.apply_app_control_bridge_patch = apply_app_control_bridge_patch
    m.apply_output_tab_command_header_patch = apply_output_tab_command_header_patch
    m.scan_task005_ui_bindings = scan_task005_ui_bindings
    m.scan_app_control_bridge = scan_app_control_bridge


def configure_app_target(app_arg: str | None) -> None:
    if not app_arg:
        return
    app = Path(app_arg).expanduser()
    m.SYSTEM_APP = app
    m.PATCH_TARGET_APP = app
    m.DEFAULT_USER_APP = app


def default_app_target() -> str | None:
    if m.SYSTEM_APP.exists():
        return None
    chatgpt_app = Path("/Applications/ChatGPT.app")
    if chatgpt_app.exists():
        return str(chatgpt_app)
    return None


def main(argv: list[str] | None = None) -> int:
    install_monkeypatches()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--app", help="Path to the Codex/ChatGPT app patch target")
    wrapper_args, passthrough = parser.parse_known_args(argv)
    configure_app_target(wrapper_args.app or default_app_target())

    if passthrough:
        return m.main(passthrough)

    try:
        payload = m.apply_patch_to_user_copy(yes=True, allow_running=True)
    except m.ControllerError as exc:
        payload = {"ok": False, "reason": exc.reason, "message": str(exc), "details": exc.details}
    except Exception as exc:
        payload = {"ok": False, "reason": "unexpected-exception", "message": str(exc)}
    payload["reportPath"] = str(m.write_report("apply-patch-current", payload))
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

use micro_http::Body;
use vmm::rpc_interface::VmmAction;
use vmm::vmm_config::agent_runtime::{AgentRuntimeConfig, AgentRuntimeState};

use crate::api_server::parsed_request::{ParsedRequest, RequestError};

pub(crate) fn parse_patch_agent_runtime(body: &Body) -> Result<ParsedRequest, RequestError> {
    let cfg = serde_json::from_slice::<AgentRuntimeConfig>(body.raw())?;

    Ok(match cfg.state {
        AgentRuntimeState::LlmWaiting => {
            ParsedRequest::new_sync(VmmAction::EnterLlmWait(cfg.into()))
        }
        AgentRuntimeState::Running => ParsedRequest::new_sync(VmmAction::ExitLlmWait),
    })
}

#[cfg(test)]
mod tests {
    use vmm::vmm_config::agent_runtime::EnterLlmWaitConfig;

    use super::*;
    use crate::api_server::parsed_request::tests::vmm_action_from_request;

    #[test]
    fn test_parse_patch_agent_runtime_request() {
        let body = r#"{
            "state": "LlmWaiting",
            "target_balloon_mib": 512,
            "acknowledge_on_stop": true
        }"#;
        assert_eq!(
            vmm_action_from_request(parse_patch_agent_runtime(&Body::new(body)).unwrap()),
            VmmAction::EnterLlmWait(EnterLlmWaitConfig {
                target_balloon_mib: Some(512),
                acknowledge_on_stop: Some(true),
            })
        );

        let body = r#"{
            "state": "Running"
        }"#;
        assert_eq!(
            vmm_action_from_request(parse_patch_agent_runtime(&Body::new(body)).unwrap()),
            VmmAction::ExitLlmWait
        );

        parse_patch_agent_runtime(&Body::new("invalid_payload")).unwrap_err();
    }
}


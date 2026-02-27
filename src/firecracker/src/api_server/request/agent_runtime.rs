// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

use micro_http::Body;
use vmm::logger::{IncMetric, METRICS};
use vmm::rpc_interface::VmmAction;
use vmm::vmm_config::agent_runtime::{
    AgentRuntimeConfig, AgentRuntimeState, SubmitLlmResponseConfig,
};

use crate::api_server::parsed_request::{ParsedRequest, RequestError};

const DEPRECATION_MESSAGE: &str =
    "PATCH /agent/runtime: target_balloon_mib and acknowledge_on_stop fields are deprecated and ignored.";

pub(crate) fn parse_patch_agent_runtime(body: &Body) -> Result<ParsedRequest, RequestError> {
    let body_json = serde_json::from_slice::<serde_json::Value>(body.raw())?;
    let cfg = serde_json::from_value::<AgentRuntimeConfig>(body_json.clone())?;

    let mut parsed_req = match cfg.state {
        AgentRuntimeState::LlmWaiting => {
            ParsedRequest::new_sync(VmmAction::EnterLlmWait(cfg.into()))
        }
        AgentRuntimeState::Running => ParsedRequest::new_sync(VmmAction::ExitLlmWait),
    };

    let has_deprecated_field = body_json.as_object().is_some_and(|obj| {
        obj.contains_key("target_balloon_mib") || obj.contains_key("acknowledge_on_stop")
    });
    if has_deprecated_field {
        METRICS.deprecated_api.deprecated_http_api_calls.inc();
        parsed_req
            .parsing_info()
            .append_deprecation_message(DEPRECATION_MESSAGE);
    }

    Ok(parsed_req)
}

pub(crate) fn parse_put_agent_runtime_response(
    body: &Body,
) -> Result<ParsedRequest, RequestError> {
    let cfg = serde_json::from_slice::<SubmitLlmResponseConfig>(body.raw())?;
    Ok(ParsedRequest::new_sync(VmmAction::SubmitLlmResponse(cfg)))
}

#[cfg(test)]
mod tests {
    use vmm::vmm_config::agent_runtime::{EnterLlmWaitConfig, SubmitLlmResponseConfig};

    use super::*;
    use crate::api_server::parsed_request::tests::{depr_action_from_req, vmm_action_from_request};

    #[test]
    fn test_parse_patch_agent_runtime_enter_llm_wait() {
        let body = r#"{
            "state": "LlmWaiting",
            "pause_on_wait": true
        }"#;
        assert_eq!(
            vmm_action_from_request(parse_patch_agent_runtime(&Body::new(body)).unwrap()),
            VmmAction::EnterLlmWait(EnterLlmWaitConfig {
                pause_on_wait: Some(true),
            })
        );
    }

    #[test]
    fn test_parse_patch_agent_runtime_enter_llm_wait_defaults() {
        let body = r#"{
            "state": "LlmWaiting"
        }"#;
        assert_eq!(
            vmm_action_from_request(parse_patch_agent_runtime(&Body::new(body)).unwrap()),
            VmmAction::EnterLlmWait(EnterLlmWaitConfig {
                pause_on_wait: None,
            })
        );
    }

    #[test]
    fn test_parse_patch_agent_runtime_deprecated_fields_are_ignored() {
        let body = r#"{
            "state": "LlmWaiting",
            "target_balloon_mib": 512,
            "acknowledge_on_stop": true
        }"#;
        assert_eq!(
            depr_action_from_req(
                parse_patch_agent_runtime(&Body::new(body)).unwrap(),
                Some(DEPRECATION_MESSAGE.to_string())
            ),
            VmmAction::EnterLlmWait(EnterLlmWaitConfig {
                pause_on_wait: None,
            })
        );
    }

    #[test]
    fn test_parse_patch_agent_runtime_exit_llm_wait() {
        let body = r#"{
            "state": "Running"
        }"#;
        assert_eq!(
            vmm_action_from_request(parse_patch_agent_runtime(&Body::new(body)).unwrap()),
            VmmAction::ExitLlmWait
        );
    }

    #[test]
    fn test_parse_patch_agent_runtime_bad_request_body() {
        assert!(matches!(
            parse_patch_agent_runtime(&Body::new("invalid_payload")),
            Err(RequestError::SerdeJson(_))
        ));

        let body = r#"{
            "pause_on_wait": true
        }"#;
        assert!(matches!(
            parse_patch_agent_runtime(&Body::new(body)),
            Err(RequestError::SerdeJson(_))
        ));

        let body = r#"{
            "state": "Paused"
        }"#;
        assert!(matches!(
            parse_patch_agent_runtime(&Body::new(body)),
            Err(RequestError::SerdeJson(_))
        ));

        let body = r#"{
            "state": "Running",
            "unexpected": true
        }"#;
        assert!(matches!(
            parse_patch_agent_runtime(&Body::new(body)),
            Err(RequestError::SerdeJson(_))
        ));
    }

    #[test]
    fn test_parse_put_agent_runtime_response() {
        let body = r#"{
            "request_id": "req-1",
            "vsock_port": 11000,
            "response": "{\"ok\":true}",
            "resume_vm": true
        }"#;
        assert_eq!(
            vmm_action_from_request(parse_put_agent_runtime_response(&Body::new(body)).unwrap()),
            VmmAction::SubmitLlmResponse(SubmitLlmResponseConfig {
                request_id: "req-1".to_string(),
                vsock_port: 11000,
                response: "{\"ok\":true}".to_string(),
                resume_vm: Some(true),
            })
        );
    }

    #[test]
    fn test_parse_put_agent_runtime_response_bad_request_body() {
        assert!(matches!(
            parse_put_agent_runtime_response(&Body::new("invalid_payload")),
            Err(RequestError::SerdeJson(_))
        ));

        let body = r#"{
            "request_id": "req-1",
            "response": "payload"
        }"#;
        assert!(matches!(
            parse_put_agent_runtime_response(&Body::new(body)),
            Err(RequestError::SerdeJson(_))
        ));
    }
}

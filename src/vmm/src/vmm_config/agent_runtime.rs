// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

use serde::{Deserialize, Serialize};

/// Desired runtime state for agent-assisted execution.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentRuntimeConfig {
    /// Target runtime state.
    pub state: AgentRuntimeState,
    /// Optional target balloon size in MiB when entering `LlmWaiting`.
    #[serde(default)]
    pub target_balloon_mib: Option<u32>,
    /// Optional hinting behavior when ending wait mode.
    #[serde(default)]
    pub acknowledge_on_stop: Option<bool>,
}

/// Valid runtime states for agent-assisted execution.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub enum AgentRuntimeState {
    /// Runtime waits for external LLM/network processing.
    LlmWaiting,
    /// Runtime is actively running in the guest.
    Running,
}

/// Configuration used when transitioning into LLM wait mode.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EnterLlmWaitConfig {
    /// Optional target balloon size in MiB.
    #[serde(default)]
    pub target_balloon_mib: Option<u32>,
    /// Optional hinting behavior when ending wait mode.
    #[serde(default)]
    pub acknowledge_on_stop: Option<bool>,
}

impl From<AgentRuntimeConfig> for EnterLlmWaitConfig {
    fn from(value: AgentRuntimeConfig) -> Self {
        Self {
            target_balloon_mib: value.target_balloon_mib,
            acknowledge_on_stop: value.acknowledge_on_stop,
        }
    }
}


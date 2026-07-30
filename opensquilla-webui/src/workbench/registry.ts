import { WorkbenchPanelRegistry } from './runtime'

/**
 * Application-level provider registry. Built-in providers register when their
 * feature module loads; future Browser, Frontend Preview, Files, Diff and
 * Terminal providers can add renderers and runtime controllers without adding
 * another panel-kind switch to the Workbench host.
 */
export const workbenchPanelRegistry = new WorkbenchPanelRegistry()

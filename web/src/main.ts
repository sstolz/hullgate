import { mount } from 'svelte';
import '@fontsource/inter/latin-400.css';
import '@fontsource/inter/latin-600.css';
import '@fontsource/jetbrains-mono/latin-400.css';
import './tokens.css';
import App from './App.svelte';

const target = document.getElementById('app');
if (target) mount(App, { target });
